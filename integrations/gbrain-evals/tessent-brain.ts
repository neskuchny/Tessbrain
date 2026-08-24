/**
 * BrainBench adapter for Tessbrain.
 *
 * Tessbrain is a Python retrieval stack (BM25 + a typed graph + RRF
 * fusion). Rewriting it in TypeScript to satisfy the adapter interface
 * would mean benchmarking a reimplementation: any drift between the copy
 * and the real system would be invisible, and the whole point of running
 * on someone else's harness is lost. So this adapter is deliberately thin.
 * It spawns the production stack as a child process and talks to it over
 * newline-delimited JSON on stdin/stdout.
 *
 * What actually answers each query is `scripts/brainbench_run.py --serve`
 * in the Tessbrain repository, which imports the same modules production
 * imports (`bm25_searcher`, `rrf_fusion`, `enumerative_detect`) with the
 * same fusion weights.
 *
 * Properties this adapter holds to:
 *   - Deterministic. The Python side sorts graph neighbours by slug before
 *     fusion, so rank never depends on hash-set iteration order. Ties in
 *     the final ranking are broken by slug, ascending.
 *   - No network. The child process opens no sockets and reads no files
 *     beyond the pages handed to it.
 *   - No gold. Only `slug`, `type`, `title`, `compiled_truth` and
 *     `timeline` cross the boundary, and only `{id, text}` per query.
 *   - Serialized. Requests are queued, so an out-of-order reply is
 *     impossible even if the runner calls query() concurrently.
 *
 * Setup: clone https://github.com/<tessent>/tessent-brain and point
 * TESSENT_REPO at it (or pass `repoPath` in the adapter config). Python
 * 3.10+ with no third-party packages is enough — the retrieval modules
 * used here are stdlib-only.
 */

import type { Adapter, AdapterConfig, BrainState, Page, Query, RankedDoc } from '../types.ts';

// ─── Bridge ─────────────────────────────────────────────────────────

interface BridgeReply {
  ok: boolean;
  error?: string;
  docs?: string[];
  pages?: number;
  name?: string;
}

/**
 * A child process speaking newline-delimited JSON, with replies matched to
 * requests by arrival order. `pending` serializes writes: each request
 * waits for the previous reply, so request N always pairs with reply N.
 */
class PythonBridge {
  private proc: ReturnType<typeof Bun.spawn> | null = null;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private decoder = new TextDecoder();
  private buffer = '';
  private pending: Promise<unknown> = Promise.resolve();
  private idle: ReturnType<typeof setTimeout> | null = null;
  /** The init payload, replayed if the child was reaped for idleness. */
  private initPayload: Record<string, unknown> | null = null;

  /**
   * How long the child may sit unused before it is reaped. The Adapter
   * interface has no teardown hook and the runner never calls snapshot(),
   * so nothing tells us the benchmark is over. A live child holds Bun's
   * event loop open, which would leave the runner hanging after it prints
   * its scorecard. Queries arrive milliseconds apart, so an idle gap this
   * long only ever happens once the run is finished.
   */
  private static readonly IDLE_MS = 4000;

  constructor(private repoPath: string, private python: string) {
    process.on('exit', () => this.close());
  }

  private spawn(): void {
    this.proc = Bun.spawn([this.python, 'scripts/brainbench_run.py', '--serve'], {
      cwd: this.repoPath,
      stdin: 'pipe',
      stdout: 'pipe',
      stderr: 'inherit',
    });
    this.reader = this.proc.stdout.getReader();
    this.buffer = '';
  }

  /** Send one request, resolve with its reply. Queued behind earlier calls. */
  send(payload: Record<string, unknown>): Promise<BridgeReply> {
    const run = this.pending.then(() => this.exchange(payload));
    // Keep the chain alive even if this call rejects, so one failure does
    // not wedge every later query behind a permanently rejected promise.
    this.pending = run.catch(() => undefined);
    return run;
  }

  private async exchange(payload: Record<string, unknown>): Promise<BridgeReply> {
    if (this.idle) clearTimeout(this.idle);

    if (!this.proc) {
      this.spawn();
      // Reaped between queries: the child is fresh and knows nothing, so
      // replay the corpus before the request that woke us.
      if (this.initPayload && payload.cmd !== 'init') {
        await this.transact(this.initPayload);
      }
    }
    if (payload.cmd === 'init') this.initPayload = payload;

    const reply = await this.transact(payload);

    this.idle = setTimeout(() => this.close(), PythonBridge.IDLE_MS);
    // An unref'd timer still fires; it just does not by itself keep the
    // loop alive. That is exactly what we want here — it fires because
    // the child is holding the loop open, and then releases it.
    this.idle.unref?.();
    return reply;
  }

  private async transact(payload: Record<string, unknown>): Promise<BridgeReply> {
    const proc = this.proc;
    if (!proc) throw new Error('tessent bridge: no child process');
    proc.stdin.write(JSON.stringify(payload) + '\n');
    await proc.stdin.flush();
    const line = await this.readLine();
    const reply = JSON.parse(line) as BridgeReply;
    if (!reply.ok) throw new Error(`tessent bridge: ${reply.error ?? 'unknown error'}`);
    return reply;
  }

  private async readLine(): Promise<string> {
    const reader = this.reader;
    if (!reader) throw new Error('tessent bridge: no output stream');
    for (;;) {
      const nl = this.buffer.indexOf('\n');
      if (nl >= 0) {
        const line = this.buffer.slice(0, nl);
        this.buffer = this.buffer.slice(nl + 1);
        if (line.trim()) return line;
        continue;
      }
      const { value, done } = await reader.read();
      if (done) throw new Error('tessent bridge closed before replying');
      this.buffer += this.decoder.decode(value, { stream: true });
    }
  }

  close(): void {
    if (this.idle) {
      clearTimeout(this.idle);
      this.idle = null;
    }
    const proc = this.proc;
    this.proc = null;
    if (!proc) return;
    // Release the stream first, then EOF, then make sure. Each step is
    // independently allowed to fail: by the time a process exit handler
    // runs, some of this may already have happened.
    try {
      this.reader?.cancel();
    } catch {
      // stream already closed
    }
    this.reader = null;
    try {
      proc.stdin.end();
    } catch {
      // already gone
    }
    try {
      proc.kill();
    } catch {
      // already gone
    }
  }
}

// ─── Adapter state ──────────────────────────────────────────────────

interface TessentState {
  bridge: PythonBridge;
  /** Slugs that exist in the corpus — guards against a stale reply. */
  known: Set<string>;
}

interface TessentConfig extends AdapterConfig {
  /** Path to a Tessbrain checkout. Defaults to $TESSENT_REPO. */
  repoPath?: string;
  /** Python executable. Defaults to $TESSENT_PYTHON or "python3". */
  python?: string;
}

export class TessentAdapter implements Adapter {
  readonly name = 'tessent-brain';

  async init(rawPages: Page[], config: TessentConfig): Promise<BrainState> {
    const repoPath = config.repoPath ?? process.env.TESSENT_REPO;
    if (!repoPath) {
      throw new Error(
        'tessent-brain adapter needs a checkout: set TESSENT_REPO or pass repoPath',
      );
    }
    const python = config.python ?? process.env.TESSENT_PYTHON ?? 'python3';
    const bridge = new PythonBridge(repoPath, python);

    // Hand over only the public fields. The runner already sanitizes, but
    // the adapter re-narrows so a future Page field cannot leak by accident.
    const pages = rawPages.map(p => ({
      slug: p.slug,
      type: p.type,
      title: p.title,
      compiled_truth: p.compiled_truth,
      timeline: p.timeline,
    }));
    await bridge.send({ cmd: 'init', pages });

    return { bridge, known: new Set(pages.map(p => p.slug)) } satisfies TessentState;
  }

  async query(q: Query, state: BrainState): Promise<RankedDoc[]> {
    const s = state as TessentState;
    const reply = await s.bridge.send({
      cmd: 'query',
      id: q.id,
      text: q.text,
      top_k: 15,
    });

    const seen = new Set<string>();
    const docs: RankedDoc[] = [];
    for (const slug of reply.docs ?? []) {
      if (!s.known.has(slug) || seen.has(slug)) continue;
      seen.add(slug);
      // Score descends with rank. The Python side has already applied RRF;
      // these values exist to satisfy the interface and are not comparable
      // across adapters, as RankedDoc documents.
      docs.push({ page_id: slug, score: 1 / (docs.length + 1), rank: docs.length + 1 });
    }
    return docs;
  }

  async snapshot(state: BrainState): Promise<string> {
    // The bridge holds an in-memory index only; there is nothing to
    // serialize, and closing here would break a later query.
    void state;
    return '';
  }
}

/** Convenience factory — construct with default config. */
export function createTessentAdapter(): TessentAdapter {
  return new TessentAdapter();
}

/**
 * Unit tests for the Tessbrain adapter.
 *
 * Covers what BrainBench's CONTRIBUTING asks of an external adapter:
 * init, query, and a deterministic tie-break. No network — the adapter
 * spawns a local Python process and speaks JSON over pipes, nothing else.
 *
 * Run: bun test integrations/gbrain-evals/tessent-brain.test.ts
 * (inside gbrain-evals, after copying this next to the adapter:
 *  bun test eval/runner/adapters/tessent-brain.test.ts)
 */

import { describe, expect, test } from 'bun:test';
import type { AdapterConfig, Page, Query } from '../types.ts';
import { TessentAdapter } from './tessent-brain.ts';

const REPO = process.env.TESSENT_REPO;

const PAGES: Page[] = [
  {
    slug: 'meetings/kickoff',
    type: 'meeting',
    title: 'Kickoff',
    compiled_truth: 'Kickoff with [Ann Lee](people/ann-lee) and [Bob Wu](people/bob-wu).',
    timeline: '',
  },
  {
    slug: 'companies/acme',
    type: 'company',
    title: 'Acme',
    compiled_truth: 'Acme was founded by [Ann Lee](people/ann-lee).',
    timeline: '',
  },
  {
    slug: 'people/ann-lee',
    type: 'person',
    title: 'Ann Lee',
    compiled_truth: 'Ann Lee works at [Acme](companies/acme).',
    timeline: '',
  },
  {
    slug: 'people/bob-wu',
    type: 'person',
    title: 'Bob Wu',
    compiled_truth: 'Bob Wu attended [Kickoff](meetings/kickoff).',
    timeline: '',
  },
];

const CONFIG: AdapterConfig = { name: 'tessent-brain' };

function q(id: string, text: string): Query {
  return { id, text } as unknown as Query;
}

// Without a checkout there is nothing to bridge to. Skip rather than fail,
// so the suite stays green for contributors who have not cloned Tessbrain.
const maybe = REPO ? describe : describe.skip;

maybe('tessent-brain adapter', () => {
  test('init ingests pages and reports readiness', async () => {
    const adapter = new TessentAdapter();
    const state = await adapter.init(PAGES, CONFIG);
    expect(state).toBeDefined();
  });

  test('query returns ranked docs with 1-based contiguous ranks', async () => {
    const adapter = new TessentAdapter();
    const state = await adapter.init(PAGES, CONFIG);
    const docs = await adapter.query(q('q-0001', 'Who attended Kickoff?'), state);

    expect(docs.length).toBeGreaterThan(0);
    docs.forEach((d, i) => expect(d.rank).toBe(i + 1));
    // Slugs must be real corpus pages, never invented.
    const slugs = docs.map(d => d.page_id);
    slugs.forEach(s => expect(PAGES.some(p => p.slug === s)).toBe(true));
    // No duplicates.
    expect(new Set(slugs).size).toBe(slugs.length);
    // The graph knows who was in the room.
    expect(slugs).toContain('people/ann-lee');
    expect(slugs).toContain('people/bob-wu');
  });

  test('repeated queries are identical (deterministic tie-break)', async () => {
    const adapter = new TessentAdapter();
    const state = await adapter.init(PAGES, CONFIG);
    const first = await adapter.query(q('q-0001', 'Who works at Acme?'), state);
    const second = await adapter.query(q('q-0001', 'Who works at Acme?'), state);
    expect(second.map(d => d.page_id)).toEqual(first.map(d => d.page_id));
  });

  test('two separate ingestions of the same corpus agree', async () => {
    // The tie-break lives on the Python side (graph neighbours are sorted
    // by slug before fusion). A fresh process must reach the same ranking,
    // otherwise hash-set iteration order is leaking into the result.
    const a = new TessentAdapter();
    const b = new TessentAdapter();
    const [sa, sb] = await Promise.all([a.init(PAGES, CONFIG), b.init(PAGES, CONFIG)]);
    const [ra, rb] = await Promise.all([
      a.query(q('q-0002', 'Who attended Kickoff?'), sa),
      b.query(q('q-0002', 'Who attended Kickoff?'), sb),
    ]);
    expect(rb.map(d => d.page_id)).toEqual(ra.map(d => d.page_id));
  });

  test('concurrent queries do not cross replies', async () => {
    const adapter = new TessentAdapter();
    const state = await adapter.init(PAGES, CONFIG);
    const [kickoff, acme] = await Promise.all([
      adapter.query(q('q-0003', 'Who attended Kickoff?'), state),
      adapter.query(q('q-0004', 'Who works at Acme?'), state),
    ]);
    // Requests are serialized inside the bridge; if replies were paired by
    // arrival rather than order, these two would swap under concurrency.
    const sequential = await adapter.query(q('q-0005', 'Who attended Kickoff?'), state);
    expect(kickoff.map(d => d.page_id)).toEqual(sequential.map(d => d.page_id));
    expect(acme.length).toBeGreaterThan(0);
  });

  test('an unknown entity yields an empty or corpus-only answer, never a crash', async () => {
    const adapter = new TessentAdapter();
    const state = await adapter.init(PAGES, CONFIG);
    const docs = await adapter.query(q('q-0006', 'Who attended Nonexistent Summit?'), state);
    expect(Array.isArray(docs)).toBe(true);
    docs.forEach(d => expect(PAGES.some(p => p.slug === d.page_id)).toBe(true));
  });
});

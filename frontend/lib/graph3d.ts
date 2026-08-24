// @ts-nocheck
/*
 * graph3d — компактный 3D-движок для графов на Three.js (грузится с CDN в window.THREE).
 * Два режима:
 *   - 'tree'  : радиальное дерево-«купол» (оргсхема: кто кому подчиняется)
 *   - 'force' : силовая сеть (граф компании: люди/команды/проекты/задачи/клиенты)
 *
 * Узлы рисуются как canvas-текстуры (аватары с инициалами, карточки, ромбы,
 * соты, бейджи) → читаемые подписи прямо на сцене. Ховер подсвечивает
 * цепочку/связи, тащить узел — двигать, фон — вращать, колесо — зум.
 *
 * Портировано из дизайн-прототипа Graph_Visualizations и обобщено под живые
 * данные (см. Graph3DView.tsx, который маппит ответы API в этот формат).
 */

/** Текст → безопасный HTML. Для всего, что попадает в innerHTML подсказок.
 *  Имена сущностей приходят из транскриптов, то есть их пишет человек. */
export const escHtml = (v: any): string =>
  String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

/** Цвет → безопасное значение для атрибута style.
 *  Пропускаем только форму цвета (#rgb, rgb(), hsl(), имя). Всё прочее —
 *  включая кавычку, точку с запятой и url(...) — заменяем на нейтральный,
 *  чтобы нельзя было закрыть атрибут и дописать свой. */
export const escCss = (v: any): string => {
  const s = String(v ?? '').trim()
  return /^(#[0-9a-f]{3,8}|(rgb|hsl)a?\([0-9.,%\s/-]+\)|[a-z]{3,20})$/i.test(s) ? s : 'currentColor'
}

export type G3DShape = 'avatar' | 'card' | 'diamond' | 'hex' | 'badge'

export interface G3DNode {
  id: string
  shape: G3DShape
  label: string
  sub?: string | null
  dept?: string
  parent?: string | null
  type?: string
  status?: string
  hue?: number
  col?: string
}

export interface G3DEdge {
  a: string
  b: string
  type?: string
}

export interface G3DConfig {
  container: HTMLElement
  tip: HTMLElement
  mode: 'tree' | 'force'
  nodes: G3DNode[]
  edges: G3DEdge[]
  R?: number
  fog?: string
  phi?: number
  theta?: number
  edgeStyle?: (type: string) => { color: string; op: number; dash: boolean }
  tooltip?: (node: any, ctx: { adjCount: number; reports: number }) => string
  filterEl?: HTMLElement | null
  // wow-слой
  glow?: boolean // неоновое свечение вокруг узлов
  pulses?: boolean // импульсы, бегущие по связям (нейро-эффект)
  starfield?: boolean // звёзды + дымка для глубины
  grid?: boolean // чертёжная сетка-пол (пресет «Чертёж»)
  gridColor?: number // основной цвет сетки
  onSelect?: (nodeId: string | null) => void // клик по узлу → инфо-панель
}

export interface G3DHandle {
  dispose: () => void
  clearSelect: () => void
}

const hsl = (h: number, s: number, l: number) => `hsl(${h}, ${s}%, ${l}%)`
const initials = (name: string) => {
  const p = (name || '').trim().split(/\s+/)
  return ((p[0] || '')[0] || '') + ((p[1] || '')[0] || '')
}

function fit(x: any, s: string, max: number) {
  if (x.measureText(s).width <= max) return s
  while (s.length > 2 && x.measureText(s + '…').width > max) s = s.slice(0, -1)
  return s + '…'
}
function rr(x: any, rx: number, ry: number, w: number, h: number, r: number) {
  x.beginPath()
  x.moveTo(rx + r, ry)
  x.arcTo(rx + w, ry, rx + w, ry + h, r)
  x.arcTo(rx + w, ry + h, rx, ry + h, r)
  x.arcTo(rx, ry + h, rx, ry, r)
  x.arcTo(rx, ry, rx + w, ry, r)
  x.closePath()
}

// ---------- textures ----------
function avatarTex(THREE: any, label: string, sub: string | null, col: string) {
  const W = 240,
    H = sub ? 300 : 248
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const x = c.getContext('2d')!
  const cx = W / 2,
    cy = 92,
    r = 72
  x.save()
  x.shadowColor = 'rgba(0,0,0,0.4)'
  x.shadowBlur = 18
  x.shadowOffsetY = 8
  x.fillStyle = '#1b1f2b'
  x.beginPath()
  x.arc(cx, cy, r, 0, 7)
  x.fill()
  x.restore()
  x.lineWidth = 7
  x.strokeStyle = col
  x.beginPath()
  x.arc(cx, cy, r, 0, 7)
  x.stroke()
  x.fillStyle = col
  x.font = '600 56px "Space Grotesk", system-ui, sans-serif'
  x.textAlign = 'center'
  x.textBaseline = 'middle'
  x.fillText(initials(label).toUpperCase(), cx, cy + 2)
  x.fillStyle = '#eef1f8'
  x.font = '600 30px "Space Grotesk", system-ui, sans-serif'
  x.fillText(fit(x, label, W - 12), cx, cy + r + 30)
  if (sub) {
    x.fillStyle = '#8b93a8'
    x.font = '400 24px "IBM Plex Mono", monospace'
    x.fillText(fit(x, sub, W - 12), cx, cy + r + 64)
  }
  const tx = new THREE.CanvasTexture(c)
  tx.anisotropy = 4
  return { tx, ratio: H / W }
}
function cardTex(THREE: any, label: string, sub: string | null, col: string) {
  const W = 330,
    H = sub ? 128 : 100
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const x = c.getContext('2d')!
  rr(x, 6, 6, W - 12, H - 12, 15)
  x.fillStyle = 'rgba(26,22,40,0.94)'
  x.fill()
  x.lineWidth = 2.5
  x.strokeStyle = col
  rr(x, 6, 6, W - 12, H - 12, 15)
  x.stroke()
  x.fillStyle = col
  rr(x, 16, 16, 16, H - 32, 6)
  x.fill()
  x.textAlign = 'left'
  x.textBaseline = 'middle'
  x.fillStyle = '#f1eefa'
  x.font = '600 30px "Space Grotesk", system-ui, sans-serif'
  x.fillText(fit(x, label, W - 70), 46, sub ? 44 : H / 2)
  if (sub) {
    x.fillStyle = '#9c93b8'
    x.font = '400 22px "IBM Plex Mono", monospace'
    x.fillText(fit(x, sub, W - 70), 46, 86)
  }
  const tx = new THREE.CanvasTexture(c)
  tx.anisotropy = 4
  return { tx, ratio: H / W }
}
function diamondTex(THREE: any, label: string, col: string) {
  const W = 200,
    H = 240
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const x = c.getContext('2d')!
  const cx = W / 2,
    cy = 82,
    s = 58
  x.save()
  x.translate(cx, cy)
  x.rotate(Math.PI / 4)
  x.shadowColor = 'rgba(0,0,0,0.4)'
  x.shadowBlur = 14
  x.shadowOffsetY = 6
  rr(x, -s / 2, -s / 2, s, s, 9)
  x.fillStyle = col
  x.globalAlpha = 0.95
  x.fill()
  x.restore()
  x.fillStyle = '#e9edf6'
  x.font = '500 25px "Space Grotesk", system-ui, sans-serif'
  x.textAlign = 'center'
  x.textBaseline = 'middle'
  x.fillText(fit(x, label, W - 6), cx, cy + s + 34)
  const tx = new THREE.CanvasTexture(c)
  tx.anisotropy = 4
  return { tx, ratio: H / W }
}
function hexTex(THREE: any, label: string, col: string) {
  const W = 240,
    H = 210
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const x = c.getContext('2d')!
  const cx = W / 2,
    cy = 98,
    r = 80
  x.beginPath()
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 6 + (i * Math.PI) / 3
    const px = cx + r * Math.cos(a),
      py = cy + r * Math.sin(a)
    i ? x.lineTo(px, py) : x.moveTo(px, py)
  }
  x.closePath()
  x.fillStyle = col
  x.globalAlpha = 0.16
  x.fill()
  x.globalAlpha = 1
  x.lineWidth = 4
  x.strokeStyle = col
  x.stroke()
  x.fillStyle = '#f1f4fb'
  x.font = '600 32px "Space Grotesk", system-ui, sans-serif'
  x.textAlign = 'center'
  x.textBaseline = 'middle'
  x.fillText(fit(x, label, r * 1.7), cx, cy)
  const tx = new THREE.CanvasTexture(c)
  tx.anisotropy = 4
  return { tx, ratio: H / W }
}
function badgeTex(THREE: any, label: string, col: string) {
  const c = document.createElement('canvas')
  const x0 = c.getContext('2d')!
  x0.font = '500 30px "IBM Plex Mono", monospace'
  const w = Math.ceil(x0.measureText(label).width) + 66,
    H = 72
  c.width = w
  c.height = H
  const x = c.getContext('2d')!
  rr(x, 4, 12, w - 8, H - 24, (H - 24) / 2)
  x.fillStyle = 'rgba(40,32,10,0.9)'
  x.fill()
  x.lineWidth = 2.5
  x.strokeStyle = col
  rr(x, 4, 12, w - 8, H - 24, (H - 24) / 2)
  x.stroke()
  x.fillStyle = col
  x.beginPath()
  x.arc(28, H / 2, 7, 0, 7)
  x.fill()
  x.fillStyle = '#f4ecd6'
  x.font = '500 30px "IBM Plex Mono", monospace'
  x.textAlign = 'left'
  x.textBaseline = 'middle'
  x.fillText(label, 46, H / 2 + 1)
  const tx = new THREE.CanvasTexture(c)
  tx.anisotropy = 4
  return { tx, ratio: H / w }
}

// Радиальное свечение (neon halo / haze) — мягкий additive-спрайт.
// Белая текстура, тонируется через SpriteMaterial.color под цвет узла.
function glowTex(THREE: any) {
  const S = 128
  const c = document.createElement('canvas')
  c.width = c.height = S
  const x = c.getContext('2d')!
  const g = x.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2)
  g.addColorStop(0, 'rgba(255,255,255,0.5)')
  g.addColorStop(0.3, 'rgba(255,255,255,0.18)')
  g.addColorStop(0.62, 'rgba(255,255,255,0.04)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  x.fillStyle = g
  x.fillRect(0, 0, S, S)
  const tx = new THREE.CanvasTexture(c)
  return tx
}
function softDot(THREE: any) {
  const S = 64
  const c = document.createElement('canvas')
  c.width = c.height = S
  const x = c.getContext('2d')!
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32)
  g.addColorStop(0, 'rgba(255,255,255,0.98)')
  g.addColorStop(0.35, 'rgba(150,210,255,0.6)')
  g.addColorStop(1, 'rgba(150,210,255,0)')
  x.fillStyle = g
  x.fillRect(0, 0, S, S)
  const tx = new THREE.CanvasTexture(c)
  return tx
}

function nodeSprite(THREE: any, n: any) {
  let res, w
  // Размеры узлов увеличены (~1.4×) для читаемости: на графе из сотен узлов
  // прежние были «мелким облаком точек».
  if (n.shape === 'avatar') {
    res = avatarTex(THREE, n.label, n.sub, n.col)
    w = n.sub ? 18 : 15
    if (n.depth != null) w *= [1.32, 1.12, 0.92, 0.84][Math.min(n.depth, 3)]
  } else if (n.shape === 'card') {
    res = cardTex(THREE, n.label, n.sub || null, n.col)
    w = 28
  } else if (n.shape === 'diamond') {
    res = diamondTex(THREE, n.label, n.col)
    w = 13
  } else if (n.shape === 'hex') {
    res = hexTex(THREE, n.label, n.col)
    w = 20
  } else {
    res = badgeTex(THREE, n.label, n.col)
    w = 17
  }
  const m = new THREE.SpriteMaterial({ map: res.tx, transparent: true, depthWrite: false })
  m._base = 1
  const sp = new THREE.Sprite(m)
  sp.scale.set(w, w * res.ratio, 1)
  return { sp, mat: m }
}

// ---------- tree layout ----------
function treeLayout(nodes: any[]) {
  const by: any = {}
  nodes.forEach((n) => {
    n.children = []
    by[n.id] = n
  })
  nodes.forEach((n) => {
    if (n.parent && by[n.parent]) by[n.parent].children.push(n)
  })
  let root = nodes.find((n) => !n.parent || !by[n.parent])
  if (!root) root = nodes[0]
  const leaves = (n: any): number => {
    if (!n.children.length) {
      n._lv = 1
      return 1
    }
    let s = 0
    n.children.forEach((c: any) => (s += leaves(c)))
    n._lv = s
    return s
  }
  const L = leaves(root) || 1
  let maxD = 0
  const assign = (n: any, d: number, start: number) => {
    n.depth = d
    maxD = Math.max(maxD, d)
    if (!n.children.length) {
      n._a = ((start + 0.5) / L) * Math.PI * 2
      return
    }
    let a = start
    n.children.forEach((c: any) => {
      assign(c, d + 1, a)
      a += c._lv
    })
    n._a = (n.children[0]._a + n.children[n.children.length - 1]._a) / 2
  }
  assign(root, 0, 0)
  const RAD = 34,
    YS = 13
  nodes.forEach((n) => {
    const r = n.depth * RAD
    n.x = r * Math.cos(n._a)
    n.z = r * Math.sin(n._a)
    n.y = (maxD / 2 - n.depth) * YS
    n.vx = n.vy = n.vz = 0
  })
}

export function buildGraph3D(cfg: G3DConfig): G3DHandle {
  const THREE = (window as any).THREE
  const { container, tip, nodes, edges, mode } = cfg
  const edgeStyle = cfg.edgeStyle || (() => ({ color: 'hsl(218,20%,60%)', op: 0.4, dash: false }))
  const R0 = cfg.R || (mode === 'tree' ? 165 : 178)
  const W = container.clientWidth || 600,
    H = container.clientHeight || 540

  const scene = new THREE.Scene()
  scene.fog = new THREE.Fog(new THREE.Color(cfg.fog || '#0c0e15'), R0 * 0.9, R0 * 2.5)
  const camera = new THREE.PerspectiveCamera(48, W / H, 0.1, 4000)
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.setPixelRatio(Math.min((window as any).devicePixelRatio || 1, 2))
  renderer.setClearAlpha(0)
  renderer.setSize(W, H)
  renderer.domElement.style.cssText = 'width:100%;height:100%;display:block'
  container.appendChild(renderer.domElement)

  const idx: any = {}
  nodes.forEach((n: any, i) => {
    n.i = i
    idx[n.id] = i
  })
  const adj = nodes.map(() => new Set<number>())
  const E = edges
    .filter((e) => idx[e.a] != null && idx[e.b] != null)
    .map((e) => ({ a: idx[e.a], b: idx[e.b], type: e.type || '' }))
  E.forEach((e) => {
    adj[e.a].add(e.b)
    adj[e.b].add(e.a)
  })
  // Важность = связность: хабы (люди/проекты с большим числом связей)
  // заметно крупнее — схема читается с первого взгляда, а не как
  // однородное облако точек. Только force: в дереве размер задаёт иерархия.
  if (mode === 'force') {
    nodes.forEach((n: any, i) => {
      n.baseS = 1 + Math.min(0.8, Math.sqrt(adj[i].size) * 0.22)
    })
  }

  if (mode === 'tree') treeLayout(nodes as any)
  else
    nodes.forEach((n: any, i) => {
      const ph = Math.acos(1 - (2 * (i + 0.5)) / nodes.length),
        th = Math.PI * (1 + Math.sqrt(5)) * i,
        rr2 = R0 * 0.32
      n.x = rr2 * Math.sin(ph) * Math.cos(th) + (Math.random() - 0.5) * 6
      n.y = rr2 * Math.cos(ph) + (Math.random() - 0.5) * 6
      n.z = rr2 * Math.sin(ph) * Math.sin(th) + (Math.random() - 0.5) * 6
      n.vx = n.vy = n.vz = 0
    })

  const glowMap = cfg.glow ? glowTex(THREE) : null
  nodes.forEach((n: any) => {
    const { sp, mat } = nodeSprite(THREE, n)
    const g = new THREE.Group()
    g.position.set(n.x, n.y, n.z)
    // неоновый ореол под узлом (additive)
    if (glowMap) {
      const gm = new THREE.SpriteMaterial({
        map: glowMap,
        color: new THREE.Color().setStyle(n.col || '#88aaff'),
        transparent: true,
        opacity: 0.5,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
      gm._base = 0.5
      const gs = new THREE.Sprite(gm)
      const baseW = (sp.scale.x || 12) * 2.4
      gs.scale.set(baseW, baseW, 1)
      g.add(gs)
      n.glowMat = gm
      n.glowSprite = gs
      n.glowBaseW = baseW
    }
    g.add(sp)
    sp.userData.idx = n.i
    n.group = g
    n.pick = sp
    n.mat = mat
    n.curO = 1
    n.tgtO = 1
    n.curS = 1
    n.tgtS = 1
    n.fire = 0
    n.active = true
    n.collapsed = false
    scene.add(g)
  })

  const lines = E.map((e: any) => {
    const st = edgeStyle(e.type)
    const geom = new THREE.BufferGeometry()
    geom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3))
    const col = new THREE.Color().setStyle(st.color)
    let line
    if (st.dash)
      line = new THREE.Line(
        geom,
        new THREE.LineDashedMaterial({ color: col, transparent: true, opacity: st.op, dashSize: 2.6, gapSize: 2 })
      )
    else line = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: st.op }))
    scene.add(line)
    return { a: e.a, b: e.b, type: e.type, line, geom, op: st.op, dash: st.dash, cur: 1, tgt: 1 }
  })

  // чертёжная сетка-пол (blueprint)
  if (cfg.grid) {
    const gc = cfg.gridColor ?? 0x2c6f88
    const grid = new THREE.GridHelper(R0 * 2.4, 24, gc, 0x16384a)
    grid.material.transparent = true
    grid.material.opacity = 0.32
    grid.position.y = -R0 * 0.6
    scene.add(grid)
  }

  // starfield + haze (глубина «in-vivo»)
  if (cfg.starfield) {
    const n = 480
    const arr = new Float32Array(n * 3)
    for (let i = 0; i < n; i++) {
      const r = R0 * 2.2 + Math.random() * R0 * 3,
        th = Math.random() * 6.283,
        ph = Math.acos(2 * Math.random() - 1)
      arr[i * 3] = r * Math.sin(ph) * Math.cos(th)
      arr[i * 3 + 1] = r * Math.cos(ph)
      arr[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th)
    }
    const sg = new THREE.BufferGeometry()
    sg.setAttribute('position', new THREE.BufferAttribute(arr, 3))
    const sm = new THREE.PointsMaterial({
      color: 0xb8ccff,
      size: 1.8,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.5,
      depthWrite: false,
      fog: false,
    })
    const pts = new THREE.Points(sg, sm)
    pts.renderOrder = -3
    scene.add(pts)
    const hazeTex = glowTex(THREE)
    const hazeCols = ['#2a3a8f', '#5a2a8f', '#1f6f7a', '#3a2f8f']
    for (let i = 0; i < 4; i++) {
      const hm = new THREE.SpriteMaterial({
        map: hazeTex,
        color: new THREE.Color(hazeCols[i]),
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        opacity: 0.1,
        fog: false,
      })
      const hs = new THREE.Sprite(hm)
      const sc = R0 * (2.6 + Math.random() * 1.8)
      hs.scale.set(sc, sc, 1)
      hs.position.set((Math.random() * 2 - 1) * R0 * 1.6, (Math.random() * 2 - 1) * R0, -R0 - Math.random() * R0)
      hs.renderOrder = -3
      scene.add(hs)
    }
  }

  // neuro-импульсы вдоль связей (action potentials)
  const pulses: any[] = []
  if (cfg.pulses && lines.length) {
    const dotTex = softDot(THREE)
    const count = Math.min(20, Math.max(6, Math.round(lines.length * 0.5)))
    for (let i = 0; i < count; i++) {
      const m = new THREE.SpriteMaterial({
        map: dotTex,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        opacity: 0,
        fog: false,
      })
      const head = new THREE.Sprite(m)
      head.scale.set(4.5, 4.5, 1)
      head.renderOrder = 1
      scene.add(head)
      pulses.push({ e: Math.floor(Math.random() * lines.length), t: Math.random(), v: 0.004 + Math.random() * 0.006, head, mat: m })
    }
  }

  const recomputeVis = () => {
    if (mode === 'tree') {
      nodes.forEach((n: any) => {
        let v = true,
          p = n
        while (p.parent && nodes[idx[p.parent]]) {
          p = nodes[idx[p.parent]]
          if (p.collapsed) {
            v = false
            break
          }
        }
        n.active = v
      })
    }
    nodes.forEach((n: any) => (n.group.visible = n.active))
    lines.forEach((e: any) => (e.line.visible = nodes[e.a].active && nodes[e.b].active))
  }
  recomputeVis()

  if (cfg.filterEl) {
    // Делегирование на КОНТЕЙНЕР, а не на чипы: React дорисовывает чипы
    // после построения сцены (гонка) — прямые addEventListener на .gv-chip
    // вешались в пустой DOM и фильтры «не работали».
    // Режим «соло»: клик по чипу типа → показать ТОЛЬКО этот тип + его прямые
    // связи (клик по «Люди» = люди и всё, с чем они связаны); повторный клик
    // или клик по другому чипу — вернуть/переключить. Понятнее, чем «скрыть
    // тип»: пользователь кликает чип, чтобы ВЫДЕЛИТЬ, а не спрятать.
    let soloType: string | null = null
    const applySolo = () => {
      if (!soloType) {
        nodes.forEach((n: any) => (n.active = true))
      } else {
        const keep = new Set<number>()
        nodes.forEach((n: any, i: number) => { if (n.type === soloType) keep.add(i) })
        lines.forEach((e: any) => {
          if (keep.has(e.a)) keep.add(e.b)
          if (keep.has(e.b)) keep.add(e.a)
        })
        nodes.forEach((n: any, i: number) => (n.active = keep.has(i)))
      }
      // подсветка чипов: активный — яркий, остальные — пригашены
      cfg.filterEl!.querySelectorAll('.gv-chip').forEach((c: any) => {
        const t = c.getAttribute('data-type')
        c.style.opacity = !soloType || t === soloType ? '1' : '0.35'
        c.style.borderColor = soloType && t === soloType
          ? 'rgba(255,255,255,0.55)' : 'rgba(255,255,255,0.16)'
      })
      recomputeVis()
      alpha = Math.max(alpha, 0.7)
    }
    const filterHandler = (ev: Event) => {
      const ch = (ev.target as HTMLElement)?.closest?.('.gv-chip') as HTMLElement | null
      if (!ch) return
      const ty = ch.getAttribute('data-type')
      if (!ty) return
      soloType = soloType === ty ? null : ty
      applySolo()
    }
    cfg.filterEl.addEventListener('click', filterHandler)

    // Hover-превью типа (из дизайн-концепта): навёл на чип — узлы этого типа
    // светятся, остальное мягко гаснет; ушёл — вернулось. Клик (solo) сильнее
    // превью; во время активного solo превью не мешает.
    const previewType = (ty: string | null) => {
      if (soloType) return
      if (!ty) {
        // selected/applyHL объявлены ниже по файлу, но замыкание выполняется
        // после полной инициализации сцены — доступ безопасен
        if (selected != null) { applyHL(selected); return }
        nodes.forEach((n: any) => { n.tgtO = 1; n.tgtS = 1 })
        lines.forEach((e: any) => (e.tgt = 1))
        return
      }
      nodes.forEach((n: any) => {
        if (!n.active) return
        const on = n.type === ty
        n.tgtO = on ? 1 : 0.12
        n.tgtS = on ? 1.12 : 1
      })
      lines.forEach((e: any) => {
        const on = nodes[e.a].type === ty || nodes[e.b].type === ty
        e.tgt = on ? 0.9 : 0.06
      })
    }
    cfg.filterEl.addEventListener('pointerover', (ev: Event) => {
      const ch = (ev.target as HTMLElement)?.closest?.('.gv-chip') as HTMLElement | null
      previewType(ch?.getAttribute('data-type') || null)
    })
    cfg.filterEl.addEventListener('pointerleave', () => previewType(null))
  }

  const ancestors = (n: any) => {
    const s = new Set<number>()
    let p = n
    while (p.parent && nodes[idx[p.parent]]) {
      p = nodes[idx[p.parent]]
      s.add(p.i)
    }
    return s
  }
  const descend = (n: any) => {
    const s = new Set<number>()
    const go = (x: any) => x.children && x.children.forEach((c: any) => { if (c.active) { s.add(c.i); go(c) } })
    go(n)
    return s
  }
  const hlSet = (i: number) => {
    const s = new Set<number>([i])
    if (mode === 'tree') {
      ancestors(nodes[i]).forEach((v) => s.add(v))
      descend(nodes[i]).forEach((v) => s.add(v))
    } else adj[i].forEach((v) => { if (nodes[v].active) s.add(v) })
    return s
  }

  let theta = cfg.theta != null ? cfg.theta : 0.6,
    phi = cfg.phi != null ? cfg.phi : 1.12,
    R = R0,
    lastI = performance.now()
  const target = new THREE.Vector3(0, 0, 0)
  const ray = new THREE.Raycaster()
  const picks = nodes.map((n: any) => n.pick)
  let hover: number | null = null,
    orbit = false,
    drag: any = null,
    px = 0,
    py = 0,
    downX = 0,
    downY = 0,
    moved = false
  const plane = new THREE.Plane(),
    tmp = new THREE.Vector3()
  const dom = renderer.domElement
  dom.style.touchAction = 'none'

  const defTip = (n: any, ctx: any) => {
    const meta =
      mode === 'tree'
        ? [n.sub, n.dept, ctx.reports ? 'в подчинении: ' + ctx.reports : ''].filter(Boolean).join(' · ')
        : [n.type, n.status, 'связей: ' + ctx.adjCount].filter(Boolean).join(' · ')
    // Подсказка уходит в innerHTML, а label/dept/status — это имена сущностей,
    // извлечённые из транскриптов и документов. То есть текст, который пишет
    // человек. Сотрудник, назвавший проект `<img src=x onerror=…>`, иначе
    // выполнил бы свой код у каждого, кто наведёт мышь на этот узел графа.
    // escHtml — для текста, escCss — для цвета (там своя граница: значение
    // подставляется внутрь атрибута style).
    return (
      '<span style="color:' +
      escCss(n.col) +
      '">●</span>&nbsp; <b style="font-family:Space Grotesk,sans-serif;font-weight:600">' +
      escHtml(n.label) +
      '</b><br><span style="opacity:.62">' +
      escHtml(meta) +
      '</span>'
    )
  }
  const tipFn = cfg.tooltip || defTip

  const ndc = (e: any) => {
    const r = dom.getBoundingClientRect()
    return {
      x: ((e.clientX - r.left) / r.width) * 2 - 1,
      y: -((e.clientY - r.top) / r.height) * 2 + 1,
      lx: e.clientX - r.left,
      ly: e.clientY - r.top,
    }
  }
  let selected: number | null = null
  const applyHL = (i: number) => {
    const s = hlSet(i)
    nodes.forEach((n: any) => {
      if (!n.active) return
      const on = s.has(n.i)
      n.tgtO = on ? 1 : 0.1
      n.tgtS = n.i === i ? 1.3 : on ? 1.04 : 1
    })
    lines.forEach((e: any) => (e.tgt = s.has(e.a) && s.has(e.b) ? 1 : 0.07))
  }
  const setHover = (i: number | null) => {
    hover = i
    if (i == null) {
      if (selected != null) {
        applyHL(selected)
      } else {
        nodes.forEach((n: any) => { n.tgtO = 1; n.tgtS = 1 })
        lines.forEach((e: any) => (e.tgt = 1))
      }
      tip.style.display = 'none'
      return
    }
    const s = hlSet(i)
    nodes.forEach((n: any) => {
      if (!n.active) return
      const on = s.has(n.i)
      n.tgtO = on ? 1 : 0.1
      n.tgtS = n.i === i ? 1.3 : on ? 1.04 : 1
    })
    lines.forEach((e: any) => (e.tgt = s.has(e.a) && s.has(e.b) ? 1 : 0.07))
    const n = nodes[i]
    const reports = mode === 'tree' ? descend(n).size : 0
    tip.innerHTML = tipFn(n, { adjCount: adj[i].size, reports })
    tip.style.display = 'block'
  }

  dom.addEventListener('pointerdown', (e: any) => {
    dom.setPointerCapture(e.pointerId)
    lastI = performance.now()
    const p = ndc(e)
    px = e.clientX
    py = e.clientY
    downX = e.clientX
    downY = e.clientY
    moved = false
    ray.setFromCamera({ x: p.x, y: p.y }, camera)
    const hit = ray.intersectObjects(picks.filter((o: any, k: number) => nodes[k].active), false)
    if (hit.length) {
      const ni = hit[0].object.userData.idx
      if (mode === 'force') {
        drag = nodes[ni]
        drag.pinned = true
      } else drag = { _node: nodes[ni], _tree: true }
    } else orbit = true
  })
  dom.addEventListener('pointermove', (e: any) => {
    const p = ndc(e)
    lastI = performance.now()
    if (Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) > 5) moved = true
    if (drag && drag._tree) {
      orbit = true
      theta -= (e.clientX - px) * 0.006
      phi -= (e.clientY - py) * 0.006
      phi = Math.max(0.22, Math.min(Math.PI - 0.22, phi))
      px = e.clientX
      py = e.clientY
    } else if (drag) {
      ray.setFromCamera({ x: p.x, y: p.y }, camera)
      camera.getWorldDirection(tmp)
      plane.setFromNormalAndCoplanarPoint(tmp, new THREE.Vector3(drag.x, drag.y, drag.z))
      const out = new THREE.Vector3()
      if (ray.ray.intersectPlane(plane, out)) {
        drag.x = out.x
        drag.y = out.y
        drag.z = out.z
        drag.vx = drag.vy = drag.vz = 0
      }
      alpha = Math.max(alpha, 0.7)
    } else if (orbit) {
      theta -= (e.clientX - px) * 0.006
      phi -= (e.clientY - py) * 0.006
      phi = Math.max(0.22, Math.min(Math.PI - 0.22, phi))
      px = e.clientX
      py = e.clientY
    } else {
      ray.setFromCamera({ x: p.x, y: p.y }, camera)
      const hit = ray.intersectObjects(picks.filter((o: any, k: number) => nodes[k].active), false)
      if (hit.length) {
        const ni = hit[0].object.userData.idx
        if (ni !== hover) setHover(ni)
        tip.style.left = p.lx + 14 + 'px'
        tip.style.top = p.ly + 14 + 'px'
        dom.style.cursor = 'pointer'
      } else {
        if (hover != null) setHover(null)
        dom.style.cursor = 'grab'
      }
    }
  })
  const select = (i: number | null) => {
    selected = i
    if (i == null) {
      setHover(null)
    } else {
      applyHL(i)
    }
    if (cfg.onSelect) cfg.onSelect(i == null ? null : (nodes[i] as any).id)
  }
  const up = () => {
    if (drag && drag._tree) {
      const n = drag._node
      if (!moved) {
        select(n.i)
        if (n.children && n.children.length) {
          n.collapsed = !n.collapsed
          recomputeVis()
        }
      }
    } else if (drag) {
      drag.pinned = false
      if (!moved) select(drag.i)
      alpha = Math.max(alpha, 0.6)
    } else if (orbit && !moved) {
      // клик по пустому фону — снять выделение
      select(null)
    }
    drag = null
    orbit = false
  }
  dom.addEventListener('pointerup', up)
  dom.addEventListener('pointercancel', up)
  dom.addEventListener('pointerleave', () => {
    if (!orbit && !drag && hover != null) setHover(null)
  })
  dom.addEventListener(
    'wheel',
    (e: any) => {
      e.preventDefault()
      R *= 1 + e.deltaY * 0.0012
      R = Math.max(R0 * 0.55, Math.min(R0 * 2.3, R))
      lastI = performance.now()
    },
    { passive: false }
  )

  // physics (force only)
  let alpha = 1.3
  const dt = 0.6,
    damp = 0.86,
    vmax = 6,
    // Центрирование сильнее на больших графах — иначе сотни узлов разлетаются
    // в разрежённое облако (мелкие точки, ничего не читается). Малые графы
    // почти не затрагиваются.
    ck = 0.02 * (1 + nodes.length / 220)
  const lenByType: any = { partof: 11, assignee: 18, member: 16, owns: 14, for: 22 }
  const step = () => {
    if (mode !== 'force') return
    const a = alpha
    for (const n of nodes as any) {
      n.fx = 0
      n.fy = 0
      n.fz = 0
    }
    for (let i = 0; i < nodes.length; i++) {
      if (!(nodes[i] as any).active) continue
      for (let j = i + 1; j < nodes.length; j++) {
        if (!(nodes[j] as any).active) continue
        const A: any = nodes[i],
          B: any = nodes[j]
        let dx = A.x - B.x,
          dy = A.y - B.y,
          dz = A.z - B.z
        let d2 = dx * dx + dy * dy + dz * dz + 0.1,
          d = Math.sqrt(d2)
        const f = (2300 * a) / d2,
          ux = dx / d,
          uy = dy / d,
          uz = dz / d
        A.fx += f * ux
        A.fy += f * uy
        A.fz += f * uz
        B.fx -= f * ux
        B.fy -= f * uy
        B.fz -= f * uz
      }
    }
    for (const e of E as any) {
      const A: any = nodes[e.a],
        B: any = nodes[e.b]
      if (!A.active || !B.active) continue
      let dx = B.x - A.x,
        dy = B.y - A.y,
        dz = B.z - A.z,
        d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.001
      const diff = (d - (lenByType[e.type] || 16)) * 0.05 * a
      const ux = dx / d,
        uy = dy / d,
        uz = dz / d
      A.fx += diff * ux
      A.fy += diff * uy
      A.fz += diff * uz
      B.fx -= diff * ux
      B.fy -= diff * uy
      B.fz -= diff * uz
    }
    // Кластеризация по типу: узлы одного типа мягко тяготеют к центроиду
    // своего типа — облако само раскладывается на области «Люди», «Проекты»,
    // «Клиенты»… (концепт-вид), а рёбра между областями читаются как потоки.
    const cts: Record<string, { x: number; y: number; z: number; n: number }> = {}
    for (const n of nodes as any) {
      if (!n.active || !n.type) continue
      const c = cts[n.type] || (cts[n.type] = { x: 0, y: 0, z: 0, n: 0 })
      c.x += n.x
      c.y += n.y
      c.z += n.z
      c.n++
    }
    const kc = 0.012 * a
    for (const n of nodes as any) {
      if (!n.active || !n.type) continue
      const c = cts[n.type]
      if (!c || c.n < 2) continue
      n.fx += (c.x / c.n - n.x) * kc
      n.fy += (c.y / c.n - n.y) * kc
      n.fz += (c.z / c.n - n.z) * kc
    }
    for (const n of nodes as any) {
      if (!n.active) continue
      n.fx -= ck * a * n.x
      n.fy -= ck * a * n.y
      n.fz -= ck * a * n.z
      if (n.pinned) continue
      n.vx = (n.vx + n.fx * dt) * damp
      n.vy = (n.vy + n.fy * dt) * damp
      n.vz = (n.vz + n.fz * dt) * damp
      const sp = Math.hypot(n.vx, n.vy, n.vz)
      if (sp > vmax) {
        const k = vmax / sp
        n.vx *= k
        n.vy *= k
        n.vz *= k
      }
      n.x += n.vx * dt
      n.y += n.vy * dt
      n.z += n.vz * dt
    }
    alpha = Math.max(0.05, alpha * 0.992)
  }

  let raf = 0
  const ep = (e: any) => e.geom.attributes.position.array
  const tick = () => {
    step()
    const T = performance.now() * 0.001
    for (const n of nodes as any) {
      n.group.position.set(n.x, n.y, n.z)
      n.curO += (n.tgtO - n.curO) * 0.15
      n.curS += (n.tgtS - n.curS) * 0.18
      n.group.scale.setScalar(n.curS * (n.baseS || 1))
      n.mat.opacity = n.mat._base * n.curO
      if (n.glowMat) {
        const idle = 0.42 + 0.12 * Math.sin(T * 0.9 + n.i)
        n.glowMat.opacity = Math.min(1, idle + n.fire * 0.8) * n.curO
        const gw = n.glowBaseW * (1 + n.fire * 0.5)
        n.glowSprite.scale.set(gw, gw, 1)
      }
      if (n.fire) n.fire *= 0.9
    }
    for (const e of lines as any) {
      if (!e.line.visible) continue
      const A: any = nodes[e.a],
        B: any = nodes[e.b],
        p = ep(e)
      p[0] = A.x
      p[1] = A.y
      p[2] = A.z
      p[3] = B.x
      p[4] = B.y
      p[5] = B.z
      e.geom.attributes.position.needsUpdate = true
      if (e.dash) e.line.computeLineDistances()
      e.cur += (e.tgt - e.cur) * 0.15
      e.line.material.opacity = e.op * e.cur
    }
    // neuro-импульсы: бегут по связи и «зажигают» целевой узел
    if (pulses.length) {
      for (const p of pulses) {
        p.t += p.v
        const e = lines[p.e]
        if (!e || !e.line.visible) {
          p.e = Math.floor(Math.random() * lines.length)
          p.t = 0
          continue
        }
        if (p.t >= 1) {
          ;(nodes[e.b] as any).fire = 1
          p.e = Math.floor(Math.random() * lines.length)
          p.t = 0
          p.v = 0.004 + Math.random() * 0.006
          continue
        }
        const A: any = nodes[e.a],
          B: any = nodes[e.b]
        p.head.position.set(A.x + (B.x - A.x) * p.t, A.y + (B.y - A.y) * p.t, A.z + (B.z - A.z) * p.t)
        const env = Math.sin(Math.max(0, Math.min(1, p.t)) * Math.PI)
        p.mat.opacity = 0.9 * env
      }
    }

    if (performance.now() - lastI > 1700 && !orbit && !drag && hover == null) theta += 0.0013
    const sp = Math.sin(phi)
    camera.position.set(R * sp * Math.sin(theta), R * Math.cos(phi), R * sp * Math.cos(theta))
    camera.lookAt(target)
    if (hover != null) {
      const n: any = nodes[hover]
      tmp.set(n.x, n.y, n.z).project(camera)
      const r = dom.getBoundingClientRect()
      tip.style.left = (tmp.x * 0.5 + 0.5) * r.width + 14 + 'px'
      tip.style.top = (-tmp.y * 0.5 + 0.5) * r.height + 14 + 'px'
    }
    renderer.render(scene, camera)
    raf = requestAnimationFrame(tick)
  }
  tick()

  const ro = new ResizeObserver(() => {
    const w = container.clientWidth,
      h = container.clientHeight
    if (!w || !h) return
    camera.aspect = w / h
    camera.updateProjectionMatrix()
    renderer.setSize(w, h)
  })
  ro.observe(container)

  return {
    dispose: () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      try {
        renderer.dispose()
        if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement)
      } catch {}
    },
    clearSelect: () => select(null),
  }
}

// Load THREE r128 once — каскад из нескольких CDN: один cdnjs бывает
// недоступен (сеть/блокировки) и 3D «просто не открывался». Пробуем по
// очереди с таймаутом; все упали → false (UI показывает понятную ошибку),
// _threeP сбрасывается — следующая попытка пробует снова.
const _THREE_CDNS = [
  'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js',
  'https://unpkg.com/three@0.128.0/build/three.min.js',
  'https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js',
]
let _threeP: Promise<boolean> | null = null
function _loadScript(src: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const s = document.createElement('script')
    s.src = src
    s.async = true
    const timer = setTimeout(() => { s.remove(); resolve(false) }, 12000)
    s.onload = () => { clearTimeout(timer); resolve(true) }
    s.onerror = () => { clearTimeout(timer); s.remove(); resolve(false) }
    document.head.appendChild(s)
  })
}
export function ensureThree(): Promise<boolean> {
  if ((window as any).THREE) return Promise.resolve(true)
  if (_threeP) return _threeP
  _threeP = (async () => {
    // Основной путь — БАНДЛ (npm three@0.128, r128-совместимый): без CDN, так
    // 3D грузится в закрытых сетях, где cdnjs/unpkg заблокированы (симптом
    // «ничего не грузится»). buildGraph3D читает глобальный THREE — проставляем.
    try {
      const mod: any = await import('three')
      ;(window as any).THREE = mod?.default && mod.default.REVISION ? mod.default : mod
      if ((window as any).THREE?.Scene) return true
    } catch (e) {
      console.warn('[graph3d] bundled three import failed, fallback to CDN:', e)
    }
    // Фолбэк на CDN (если бандл почему-то недоступен).
    for (const src of _THREE_CDNS) {
      if (await _loadScript(src)) return true
      console.warn('[graph3d] CDN failed, trying next:', src)
    }
    _threeP = null // всё упало — позволяем повторить позже
    return false
  })()
  return _threeP
}

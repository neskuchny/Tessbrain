/**
 * timeline3d — «нить времени» на Three.js (window.THREE, тот же CDN, что graph3d).
 *
 * Светящаяся нить из прошлого в будущее: точки-версии (сферы), события
 * (октаэдры: встречи/решения), прогнозы (полупрозрачные «призраки» справа).
 * Управление: драг — вращение, колесо — зум, клик по точке — колбэк наружу
 * (карточка версии/события рисуется в React, не в canvas).
 */

export interface TL3DItem {
  kind: 'version' | 'event' | 'future' | 'now'
  date: string        // подпись под точкой
  title: string       // тултип
  color: string       // css hex
  refIdx: number      // индекс в исходном массиве (для onSelect)
}

export interface TL3DHandle { dispose: () => void }

export function buildTimeline3D(opts: {
  container: HTMLElement
  items: TL3DItem[]
  onSelect?: (item: TL3DItem | null) => void
}): TL3DHandle | null {
  const THREE = (window as any).THREE
  if (!THREE || !opts.container) return null
  const { container, items } = opts

  const W = container.clientWidth || 800
  const H = container.clientHeight || 280

  const scene = new THREE.Scene()
  scene.fog = new THREE.Fog(0x0c0a1d, 60, 260)
  const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 600)
  camera.position.set(0, 14, 66)

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.setSize(W, H)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  container.appendChild(renderer.domElement)

  scene.add(new THREE.AmbientLight(0xffffff, 0.75))
  const pt = new THREE.PointLight(0x9b7bff, 1.1, 300)
  pt.position.set(0, 40, 60)
  scene.add(pt)

  const world = new THREE.Group()
  scene.add(world)

  // ── раскладка: точки вдоль X по порядку, лёгкая синусоида по Y/Z ──
  const n = items.length
  const span = Math.max(60, n * 9)
  const pos: any[] = items.map((_, i) => {
    const t = n > 1 ? i / (n - 1) : 0.5
    const x = -span / 2 + t * span
    return new THREE.Vector3(x, Math.sin(t * Math.PI * 1.6) * 4, Math.cos(t * Math.PI * 1.1) * 5)
  })

  // ── нить: светящаяся труба по кривой Катмулла-Рома ──
  if (pos.length >= 2) {
    const curve = new THREE.CatmullRomCurve3(pos)
    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, Math.max(64, n * 10), 0.35, 10, false),
      new THREE.MeshPhongMaterial({ color: 0x6d5bd0, emissive: 0x2c1f66, transparent: true, opacity: 0.85 })
    )
    world.add(tube)
    // ореол
    const halo = new THREE.Mesh(
      new THREE.TubeGeometry(curve, Math.max(64, n * 10), 0.9, 8, false),
      new THREE.MeshBasicMaterial({ color: 0x7b5bff, transparent: true, opacity: 0.10 })
    )
    world.add(halo)
  }

  // ── точки ──
  const pickables: any[] = []
  const sprites: any[] = []
  const mkLabel = (text: string, big = false) => {
    const c = document.createElement('canvas')
    const ctx = c.getContext('2d')!
    const fs = big ? 30 : 22
    ctx.font = `${fs}px 'IBM Plex Mono', monospace`
    const w = Math.ceil(ctx.measureText(text).width) + 16
    c.width = w; c.height = fs + 14
    const ctx2 = c.getContext('2d')!
    ctx2.font = `${fs}px 'IBM Plex Mono', monospace`
    ctx2.fillStyle = big ? '#7dffc9' : 'rgba(200,206,230,0.85)'
    ctx2.fillText(text, 8, fs + 2)
    const tex = new THREE.CanvasTexture(c)
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }))
    sp.scale.set(c.width / 22, c.height / 22, 1)
    return sp
  }

  items.forEach((it, i) => {
    const col = new THREE.Color(it.color)
    let mesh: any
    if (it.kind === 'event') {
      mesh = new THREE.Mesh(
        new THREE.OctahedronGeometry(0.9),
        new THREE.MeshPhongMaterial({ color: col, emissive: col.clone().multiplyScalar(0.35) })
      )
    } else if (it.kind === 'future') {
      mesh = new THREE.Mesh(
        new THREE.SphereGeometry(1.1, 18, 18),
        new THREE.MeshPhongMaterial({ color: col, transparent: true, opacity: 0.38, emissive: col.clone().multiplyScalar(0.3) })
      )
    } else {
      const r = it.kind === 'now' ? 1.25 : 1.0
      mesh = new THREE.Mesh(
        new THREE.SphereGeometry(r, 22, 22),
        new THREE.MeshPhongMaterial({ color: col, emissive: col.clone().multiplyScalar(it.kind === 'now' ? 0.55 : 0.3) })
      )
    }
    mesh.position.copy(pos[i])
    mesh.userData = it
    world.add(mesh)
    pickables.push(mesh)

    if (it.date && (it.kind !== 'event' || n <= 14)) {
      const sp = mkLabel(it.kind === 'now' ? 'СЕЙЧАС' : it.date, it.kind === 'now')
      sp.position.set(pos[i].x, pos[i].y - 3.6, pos[i].z)
      world.add(sp)
      sprites.push(sp)
    }
  })

  // звёздный фон
  const starGeo = new THREE.BufferGeometry()
  const starPos = new Float32Array(360 * 3)
  for (let i = 0; i < 360; i++) {
    starPos[i * 3] = (Math.random() - 0.5) * 420
    starPos[i * 3 + 1] = (Math.random() - 0.5) * 220
    starPos[i * 3 + 2] = (Math.random() - 0.5) * 320
  }
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3))
  scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0x8a92c9, size: 0.6, transparent: true, opacity: 0.5 })))

  // ── управление: драг — вращение мира, колесо — зум ──
  let dragging = false, px = 0, py = 0
  let rotY = 0, rotX = -0.12, dist = 66
  const el = renderer.domElement
  const onDown = (e: PointerEvent) => { dragging = true; px = e.clientX; py = e.clientY }
  const onMove = (e: PointerEvent) => {
    if (!dragging) return
    rotY += (e.clientX - px) * 0.005
    rotX = Math.max(-1.1, Math.min(0.5, rotX + (e.clientY - py) * 0.004))
    px = e.clientX; py = e.clientY
  }
  const onUp = () => { dragging = false }
  const onWheel = (e: WheelEvent) => {
    e.preventDefault()
    dist = Math.max(22, Math.min(180, dist + e.deltaY * 0.06))
  }
  el.addEventListener('pointerdown', onDown)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  el.addEventListener('wheel', onWheel, { passive: false })

  // клик → raycast → onSelect
  const ray = new THREE.Raycaster()
  const mouse = new THREE.Vector2()
  const onClick = (e: MouseEvent) => {
    const r = el.getBoundingClientRect()
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
    ray.setFromCamera(mouse, camera)
    const hit = ray.intersectObjects(pickables, false)
    opts.onSelect?.(hit.length ? (hit[0].object.userData as TL3DItem) : null)
  }
  el.addEventListener('click', onClick)

  // resize
  const onResize = () => {
    const w = container.clientWidth, h = container.clientHeight
    if (!w || !h) return
    camera.aspect = w / h
    camera.updateProjectionMatrix()
    renderer.setSize(w, h)
  }
  const ro = new ResizeObserver(onResize)
  ro.observe(container)

  let raf = 0
  let disposed = false
  const clock = new THREE.Clock()
  const animate = () => {
    if (disposed) return
    raf = requestAnimationFrame(animate)
    const t = clock.getElapsedTime()
    world.rotation.y = rotY + (dragging ? 0 : Math.sin(t * 0.14) * 0.05)
    world.rotation.x = rotX
    camera.position.set(0, 14, dist)
    camera.lookAt(0, 0, 0)
    // пульс «СЕЙЧАС»
    pickables.forEach((m) => {
      if ((m.userData as TL3DItem).kind === 'now') {
        const s = 1 + Math.sin(t * 2.4) * 0.05
        m.scale.set(s, s, s)
      }
    })
    renderer.render(scene, camera)
  }
  animate()

  return {
    dispose: () => {
      disposed = true
      cancelAnimationFrame(raf)
      ro.disconnect()
      el.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('click', onClick)
      renderer.dispose()
      container.removeChild(el)
    },
  }
}

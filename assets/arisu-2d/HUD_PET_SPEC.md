# Arisu HUD / Pet — Spec untuk Project Computer Use di Desktop

Tujuan: Arisu tampil sebagai **dekorasi HUD + pet** saat agent computer-use
bekerja di desktop (ala Codex / Claude app), tanpa mengganggu pekerjaan.
Dokumen ini adalah kontrak awal untuk project Antigravity di PC kantor
(Windows) dan bisa dipakai ulang di Linux/macOS.

**Keputusan 19 Nov: fokus 2D.** Resource 3D/VRM tidak disertakan; HUD/pet
memakai sprite dari `2d/layers` dan aset jam.

## 1. Prinsip UX

1. **Non-intrusive.** HUD tidak mencuri fokus, tidak muncul di tengah layar,
   tidak menutupi menu/taskbar. Default pojok kanan-bawah.
2. **Click-through.** Klik di area transparan diteruskan ke aplikasi di bawah.
   Hanya badan Arisu/tombol kecil yang menerima klik.
3. **Selalu terlihat, tidak selalu ramai.** 1 karakter + 1 bubble pendek;
   tidak ada panel besar kecuali dibuka.
4. **Status jujur.** Label mengikuti pekerjaan nyata (mencari, mengerjakan,
   menyiapkan suara), bukan "proses..." generik.
5. **Bisa disembunyikan.** Hotkey global hide/show; tidak ada animasi yang
   memblokir input.
6. **Reduced motion.** Hormati preferensi OS; blink/idle boleh dimatikan.
7. **Target sentuh ≥44×44 px** untuk tombol; teks bubble ≥13 px, kontras WCAG AA.
8. **Privasi.** HUD tidak menyimpan screenshot; computer-use capture berjalan
   di proses agent, bukan di renderer HUD.

## 2. State machine

| State | Kapan | Ekspresi | Label bubble |
|---|---|---|---|
| `idle` | menunggu | neutral / gentle-smile, blink loop | — (atau sapaan singkat) |
| `listening` | mic aktif | playful-wink / focused | "Mendengarkan…" |
| `thinking` | model mulai | focused | "Arisu berpikir…" |
| `searching` | web search | focused | "Sedang mencari…" |
| `working` | tool/terminal/file | focused + data slate | "Sedang mengerjakan…" |
| `speaking` | audio keluar | smile/laugh + viseme | "Arisu bicara…" |
| `success` | selesai | soft-laugh | ringkasan 1 baris |
| `error` | gagal | surprised | "Gagal: <sebab singkat>" |
| `shy` | dipuji/ditease | shy | — |
| `sleepy` | idle lama | sleepy | — |

Transisi tidak boleh lompat liar: `idle → listening → thinking → searching/
working → speaking → success → idle`. Error boleh dari state mana pun.

## 3. Layout & visual

- **Ukuran karakter:** 220–320 px tinggi (default 260 px), skala mengikuti DPI.
- **Posisi:** pojok kanan-bawah, offset 24 px dari tepi; drag untuk pindah;
  posisi disimpan per-monitor.
- **Dua gaya yang didukung:**
  1. **HUD card (cepat):** panel gelap rounded 432×514 dari aset jam,
     bubble di bawah/atas. Cocok karena aset jam sudah satu paket.
  2. **Transparent sprite (disarankan):** karakter RGBA tanpa panel, shadow
     lembut, bubble terpisah. Butuh komposisi `2d/layers` dulu.
- **Bubble:** maksimal 2 baris, auto-hide 4–6 dtk, tail kecil ke karakter;
  teks tidak menutupi wajah. Warna: navy `#1C3F7A` 92% + teks putih.
- **Aksen:** garis/glow ice `#E0E8F5`, blush `#FCE3E6`; hindari gradien neon.
- **Multi-monitor:** pilih monitor aktif; jangan pindah monitor saat agent
  bekerja. Simpan posisi per monitor (DPI-aware).
- **Topmost:** `alwaysOnTop` level normal; jangan di atas layar penuh eksklusif
  (game/video) — turun otomatis saat fullscreen terdeteksi.

## 4. Aset yang dipakai

| Kebutuhan | Path | Catatan |
|---|---|---|
| 8 ekspresi + blink | `watch/assets/default.w432-s/arisu_*.png` | 432×514, **opaque** panel gelap |
| Face crop kecil | `watch/assets/default.w432-s/face_*.png` | 47×47 opaque (untuk badge/ikon) |
| Scrim bubble | `watch/assets/default.w432-s/scrim.png` | RGBA gradien |
| Layer transparan | `2d/layers/*.png` | RGBA; urutan di `2d/LAYER_MAP.md`; perlu align |
| Turnaround/detail | `2d/turnaround-*.jpg`, `2d/detail-*.jpg` | referensi desain |
| Palette | `palette.json`, `2d/detail-palette-materials.jpg` | sumber warna |

**Rekomendasi produksi:** pakai layer kit untuk sprite transparan. Ekspresi
dibentuk dari kombinasi layer:
- neutral: eyes-open, mouth-closed, alis netral.
- smile/laugh: eyes-open/closed + mouth-A/E.
- wink: eyes-wink + mouth-closed/E.
- focused: eyes-half + mouth-closed.
- surprised: eyes-open + mouth-O.
- shy: eyes-half + blush + mouth-closed.
- sleepy: eyes-closed + mouth-closed.
- blink: eyes-half → eyes-closed → eyes-open (60–90 ms per frame, acak 3–7 dtk).

Viseme untuk speaking: A/E/I/O/U/closed, 80–120 ms per frame dari amplitudo
audio (RMS) kalau tersedia; kalau tidak, pola acak halus.

## 5. Arsitektur (Windows office PC)

Pilihan utama: **Tauri** (ringan, installer kecil) atau **Electron** (lebih
cepat dikembangkan). WPF juga bisa, tetapi animasi sprite lebih repot.

```
[Agent computer-use] --events--> [HUD bridge WS/IPC] --> [Overlay window]
        |                                                    |
        +-- screen capture / aksi (proses terpisah)           +-- render Arisu
```

- **Overlay window:** frameless, transparent, `alwaysOnTop`, tanpa taskbar,
  tanpa fokus (`focusable: false`), click-through default.
- **Click-through:** `setIgnoreMouseEvents(true, {forward:true})`; aktifkan
  interaksi hanya saat kursor di badan Arisu/tombol (hit-test region).
- **Hotkey global:** `Ctrl+Alt+A` hide/show, `Ctrl+Alt+P` pause pet,
  `Ctrl+Alt+M` mute suara.
- **Bridge:** WebSocket lokal (ws://127.0.0.1:PORT) atau IPC Tauri. Agent
  mengirim event JSON; HUD tidak pernah memanggil tool.
- **Contract:**

```json
{"type":"state","state":"searching","text":"Sedang mencari…","mood":"focused"}
{"type":"speak","text":"Antam hari ini Rp2.517.000","audio":"file:///.../tts.mp3"}
{"type":"progress","label":"Mengirim suara… 3/8","value":0.38}
{"type":"done","text":"Selesai.","mood":"smile"}
{"type":"error","text":"Upload timeout"}
```

- **Audio:** putar TTS di HUD (hidden `<audio>`) atau serahkan ke agent;
  jangan dua-duanya.
- **Screen capture:** di proses agent (bukan renderer HUD). HUD hanya menerima
  state; tidak menyimpan gambar layar.

## 6. Catatan PC kantor (Windows)

- Tanpa admin: pakai portable/`--user` install; PowerShell + `.cmd/.ps1`
  turnkey (bypass execution policy) seperti pola yang sudah dipakai.
- Transparan + always-on-top bekerja di Windows 10/11; set DPI awareness
  per-monitor agar tidak blur di layar 125%/150%.
- Jangan pakai fullscreen eksklusif; kalau terdeteksi, turunkan topmost.
- Startup: shortcut di Startup folder (tanpa admin), bukan service.
- Ingat aturan kantor: jangan pasang Tailscale di PC kantor; HUD berjalan
  lokal, tanpa port publik.

## 7. Milestone

1. **P0 — Overlay hidup:** window transparan always-on-top, sprite idle +
   blink, hotkey hide/show, posisi tersimpan. (Bisa pakai aset jam langsung.)
2. **P1 — State nyata:** event bridge dari agent; label searching/working/
   speaking; error state.
3. **P2 — Suara & bubble:** TTS + viseme; bubble auto-hide; progress chunk.
4. **P3 — Transparan penuh:** komposisi layer kit → sprite RGBA; shadow,
   idle sway, blink halus.

## 8. Pitfalls

- **Topmost di atas fullscreen** membuat video/game tidak bisa dikontrol;
  deteksi fullscreen dan turunkan.
- **Click-through bocor:** kalau seluruh window menerima klik, user tidak
  bisa mengklik aplikasi di bawahnya. Hit-test ketat.
- **DPI blur:** render pada skala per-monitor; jangan stretch bitmap.
- **CPU/GPU:** animasi CSS transform/opacity, bukan repaint besar; batasi 30 fps
  saat idle.
- **Fokus:** `focusable:false` + `skipTaskbar`; jangan pernah `focus()`.

# Arisu Layer Map — 2D Parts

Sumber:
- `layer-sheet-head.jpg` (2752×1536, 459.906 B)
- `layer-sheet-outfit.jpg` (2752×1536, 404.318 B)

## Head sheet
- Hair back (sudah termasuk crown bow)
- Hair front & sides
- Cat ears kiri/kanan
- Bald face base (tanpa nose/blush/ekspresi)
- Eyebrows
- Eyes: open, closed, half-open, wink (ada dua set; pakai satu)
- Mouth visemes: A, I, U, E, O, closed (ada dua set; pakai satu)
- Silver ribbon choker
- Halo hair ornament
- Back hair crown bow (duplikat dengan bow di hair back)
- Tail + ribbon
- Palette (bukan layer)

## Outfit sheet
- Body base (nude, wajah kosong)
- White capelet
- Sapphire bodice
- Detached sleeves kiri/kanan
- Skirt front
- Skirt back
- White thigh-highs kiri/kanan
- White high heels kiri/kanan
- Data slate

## Layer order (bottom → top)
1. Body base
2. Skirt back
3. Skirt front
4. Thigh-highs kiri/kanan
5. White heels
6. Sapphire bodice
7. White capelet
8. Detached sleeves
9. Face base
10. Eyes
11. Eyebrows
12. Mouth viseme
13. Hair back
14. Hair front
15. Cat ears
16. Choker
17. Halo
18. Crown bow (pilih salah satu: bow di hair back ATAU layer terpisah)
19. Tail + ribbon
20. Data slate (hanya state work)

## Catatan assembly
- Parts di atas background putih; untuk layer, hapus background dengan border flood-fill
  (putih di luar part), bukan threshold global — capelet/thigh-highs/heels juga putih.
- Skala part di sheet belum tentu sama dengan body base; align pakai turnaround/detail.
- Hair back sudah punya bow; kalau mau bow terpisah, hapus bow dari hair back dulu.
- Eyes/visemes ada dua set (center & kanan); pilih satu, jangan dobel.
- Face base kosong: tambah nose line/blush dari detail sheet/expression sheet.
- Tail root di lower back; tail di atas rok; untuk VRM taruh tail bone di sacrum.
- Data slate hanya untuk pose Work; bukan layer default.

## Kit built — 20 Okt 2026
- **32 PNG transparan** di `layers/` (RGBA; background sudah dihapus).
- `layers-manifest.json` — nama file, dimensi, urutan layer.
- `layers-contact.jpg` — contact sheet untuk cek visual.
- Catatan kit:
  - Eyes/visemes adalah **satu mata/satu mulut**; mirror untuk sisi lain.
  - `head-hair-back` sudah termasuk crown bow; `head-crown-bow` terpisah opsional —
    pilih salah satu, jangan dobel.
  - `head-face-base` dan `outfit-body-base` kosong/nude; nose/blush/ekspresi diambil
    dari detail/expression sheet.
  - Layer belum di-scale/align; pakai A-pose/turnaround sebagai acuan.
  - PNG kit bisa diimpor ke GIMP/Inochi2D; PSD/ORA belum dibuat (tidak ada writer PSD
    di VM). Kalau perlu PSD, impor PNG + manifest di GIMP lalu save as PSD.

## Next
- Opsional: Inochi2D rig (blink, breath, tail sway).
- VRM: `vrm/VRM_SPEC.md` sudah dibuat; actual mesh belum ada — butuh Blender/VRoid
  di PC kantor (VM tidak punya Blender). Pakai turnaround/detail/expression + layer kit
  sebagai tekstur/referensi.

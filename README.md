# Arisu Computer Use

**Arisu Computer Use** adalah plugin **Computer Use** untuk **Google Antigravity** di Windows 10/11 yang dilengkapi **Arisu 2D Desktop Pet HUD (`PySide6`)**, otomasi desktop native (`Win32` + `UI Automation` + `Vision WebP`), serta *Native Python CDP Browser Fast-Lane* untuk Google Chrome / Microsoft Edge.

Mendukung layar laptop tunggal (termasuk *High-DPI scaling* 100%–200%) maupun setup multi-monitor secara otomatis.

---

## Cara Pasang Super Mudah (Tanpa Akun GitHub & Tanpa Git CLI)

Cukup buka **Google Antigravity** di laptop Windows kamu, lalu kirim **1 prompt ini** di chat:

> **"Unduh repo publik `https://github.com/caerdfasgrae/arisu-computer-use` (bisa pakai ZIP `https://github.com/caerdfasgrae/arisu-computer-use/archive/refs/heads/main.zip` ke folder `%USERPROFILE%\Documents\arisu-computer-use` jika `git` belum ada), lalu jalankan `powershell -ExecutionPolicy Bypass -File .\install.ps1` di dalam foldernya sampai lulus tes."**

Setelah selesai, **tutup dan buka ulang Antigravity 1x** (atau *reload window*), lalu kamu bisa langsung memanggilnya dengan mengetik:

```text
/arisu-computer-use
```

---

## Fitur Utama

- **Arisu 2D Desktop Pet HUD (`PySide6`)**: Karakter 2D Arisu muncul otomatis di pojok kanan-bawah saat sesi aktif, menampilkan status & ekspresi, tombol **PAUSE** (`Ctrl+Alt+P`) dan **STOP** (`Ctrl+Alt+Esc`), serta **otomatis tertutup** saat tugas selesai.
- **High-DPI & Multi-Monitor Ready**: Otomatis menyesuaikan posisi HUD dan indikator kursor pada layar 100%, 125%, 150%, hingga 200% scaling.
- **4-Tier Adaptive Engine**:
  1. **CLI/PowerShell** untuk operasi file lokal dan informasi sistem.
  2. **Native Python CDP Browser Fast-Lane** (`arisu_cu_browser_action`) untuk otomasi web cepat pada profil Chrome/Edge terisolasi.
  3. **Compound Mouse + Keyboard Action** (`click` + `type_text` + `press_enter` dalam satu langkah, termasuk *auto-clipboard paste* untuk teks panjang).
  4. **Win32 + UIA + Vision `.webp`** untuk aplikasi desktop Windows native.
- **Safety & Co-Pilot Hand-Off**:
  - Tombol fisik **STOP** di HUD (atau `Ctrl+Alt+Esc`) menghentikan seluruh aksi secara instan dan mengirim laporan `USER_ABORTED` ke Antigravity.
  - Mode **Hand-Off** untuk login, 2FA/OTP, CAPTCHA, atau pembayaran agar kendali diserahkan ke pengguna dengan aman.
  - Blokir otomatis (*denylist*) untuk aplikasi sensitif (password manager, perbankan, file `.env`/`.ssh`).

---

## Prasyarat

- **Windows 10 / 11 (64-bit)**
- **Google Antigravity**
- **Google Chrome** atau **Microsoft Edge** (untuk mode Browser CDP)
- **Python 3.11+** *(otomatis diinstal via `winget` oleh `install.ps1` jika belum ada)*

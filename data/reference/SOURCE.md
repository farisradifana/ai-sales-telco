# Sumber master wilayah Jawa Timur

- Repository: https://github.com/cahyadsn/wilayah (lisensi MIT, lihat `LICENSE.cahyadsn-wilayah.txt`)
- Dasar data: Kepmendagri No 300.2.2-2430 Tahun 2025 (menurut deskripsi repository)
- Commit dipin: `d68e8d5516f969d1905d0b2940f20034becb0db7`, file `db/wilayah.sql`
- SHA-256 sumber: `c4c3396d9380d4edee072af1d9dff83573b574d7cd00a6562cf82e200e954031`
- Ekstrak: `python -I scripts/extract_jatim.py <wilayah.sql> data/reference/jatim_wilayah.csv --commit <sha>`
  (file SQL dibaca sebagai data, tidak dieksekusi); hasil 1 provinsi, 38 kabupaten/kota, 666 kecamatan.
- SHA-256 `jatim_wilayah.csv`: `01f671e3d2c8bf969cb79baeffc5a93a4adc71f1c839a16c900bd8cbb579919d`

Wilayah administrasi nyata; **outlet, pemilik, alamat, sales, transaksi dan pelanggan tetap fiktif**. Outlet tidak
memiliki koordinat (NULL) agar tidak menyiratkan lokasi usaha nyata. Desa/kelurahan tidak dipakai.

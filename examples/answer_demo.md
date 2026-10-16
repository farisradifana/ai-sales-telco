# Contoh jawaban agent berbasis hasil demo

**Pertanyaan:** Mengapa omzet OUT-0001 turun?

Sell-out Outlet Fiktif OUT-0001 pada September 2026 sebesar Rp2.159.800, turun Rp5.173.100 (-70.55%) dibanding Juni 2026. Kedua periode berisi 30 hari dan input tercatat lengkap. Periode akhir eksklusif: Juni [1 Juni, 1 Juli), September [1 September, 1 Oktober), WIB.

Kontributor negatif terbesar adalah Voucher Fiktif Nova 8GB 30 Hari: omzet Rp1.870.000 menjadi Rp0, sehingga menyumbang penurunan Rp1.870.000. Ini adalah bucket produk keluar/tidak terjual pada periode pembanding, bukan bukti produknya dihentikan atau stoknya habis. SKU lain dan efek volume/harga ada pada JSON evidence.

Kunjungan yang direncanakan pada September tidak tercatat terlaksana. Pola ini patut diverifikasi, tetapi belum membuktikan bahwa kunjungan menyebabkan penurunan omzet. Stockout tidak diukur; harga pesaing dan gangguan jaringan juga tidak tersedia.

**Follow-up:** Apa yang harus dilakukan?

1. Supervisor menjadwalkan kunjungan verifikasi OUT-0001, mengecek kendala input, permintaan dan ketersediaan SKU utama. Evaluasi 7 hari; bandingkan dengan outlet kontrol yang sepadan.
2. Sales dan trade marketing mengaudit SKU Voucher Nova 8GB yang menyumbang penurunan terbesar. Tentukan apakah masalahnya permintaan, input, harga atau ketersediaan sebelum mengubah replenishment/promo.
3. Pantau omzet net per hari dan penyelesaian kunjungan; batasi biaya diskon. Belum ada estimasi uplift yang tervalidasi.

Konteks yang dipertahankan: outlet ID1, sell-out, baseline Juni 2026 dan current September 2026; sumber telco.diagnose versi diagnose-v1. Ini data skenario fiktif seed42, bukan kesimpulan bisnis dunia nyata.

Evidence: outlet_evidence.json dan outlet_recommendation.json. API produksi menambahkan evidence_id, digest dan correlation_id; narasi ini tidak mengarang ID.

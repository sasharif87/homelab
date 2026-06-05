#!/usr/bin/env python3
"""Generate Homebox v0.25 import CSV from structured hardware data.

Homebox parses the import as comma-CSV (quoted), NOT TSV. Using DictWriter
guarantees every row has the exact column count and correct alignment.
Re-run after editing data; re-import is idempotent via HB.import_ref.
"""
import csv

COLS = [
    "HB.import_ref", "HB.location", "HB.labels", "HB.quantity", "HB.name",
    "HB.description", "HB.manufacturer", "HB.model_number", "HB.serial_number",
    "HB.purchase_price", "HB.purchase_from", "HB.purchase_time", "HB.notes",
    "HB.field.ZFS Pool", "HB.field.Health",
]

# Short keys -> HB.* columns, in order
KEYS = ["ref", "loc", "labels", "qty", "name", "desc", "mfr", "model",
        "serial", "price", "from", "time", "notes", "pool", "health"]

PN = "Homelab / Primary Node"
JBOD = "Homelab / KTN-STL4 JBOD"
SPARE = "Homelab / Spares & Shelf"
WPC = "Homelab / Workout PC (break-glass)"
GR = "Office / Gaming Rig"
MC = "Micro Center"

# Each row: values in KEYS order. Missing trailing values default to "".
def row(ref, loc, labels, qty, name, desc="", mfr="", model="", serial="",
        price="", frm="", time="", notes="", pool="", health="Healthy"):
    return dict(zip(COLS, [ref, loc, labels, qty, name, desc, mfr, model,
                           serial, price, frm, time, notes, pool, health]))

ITEMS = [
    row("hw-cpu", PN, "CPU", 1, "AMD Ryzen 9 5900XT",
        "16C/32T Zen 3, AM4, 3.3/4.9GHz", "AMD", "5900XT", notes="Primary node CPU"),
    row("hw-mb", PN, "Motherboard", 1, "ASUS PRIME B550-PLUS AC-HES",
        "B550 AM4. One free slot (PCIEX16_2). Onboard Realtek GbE", "ASUS",
        "PRIME B550-PLUS AC-HES",
        notes="PCIe: GPU=PCIEX16_1, X540=_4, 9207-8e=_5, 9207-8i=_3"),
    row("hw-ram", PN, "RAM", 4, "Corsair Vengeance LPX 32GB DDR4",
        "4x32GB = 128GB total, non-ECC. Rated DDR4-3600 C18, running at 3200",
        "Corsair", "CMK64GX4M2D3600C18", time="2026-05-26",
        notes="2x 2x32GB kits. VM 96GB / Proxmox+ARC 32GB"),
    row("hw-gpu", PN, "GPU", 1, "NVIDIA Quadro RTX 5000",
        "16GB GDDR6 Turing (TU104GL). VFIO passthrough to VM101", "NVIDIA",
        "Quadro RTX 5000", price="360",
        notes="PCI 09:00.0. Driver 570.x. Est. range was $320-400 (replaced dead GTX 1070)"),
    row("hw-nic-x540", PN, "NIC", 1, "Intel X540-T2 10GbE",
        "Dual-port 10GbE copper RJ45", "Intel", "X540-T2", price="30",
        notes="PCI 04:00.0/.1. Installed (older docs wrongly said shelved). Est. range $20-40"),
    row("hw-hba-8i", PN, "HBA;SAS", 1, "LSI 9207-8i", "Internal SAS HBA, IT mode",
        "Broadcom/LSI", "SAS9207-8i", price="32",
        notes="PCI 07:00.0. Serves apps SSD + logs mirror. Est. range $25-40"),
    row("hw-hba-8e", PN, "HBA;SAS", 1, "LSI 9207-8e",
        "External SAS HBA to KTN-STL4, IT mode", "Broadcom/LSI", "SAS9207-8e",
        price="40", frm="eBay",
        notes="PCI 05:00.0. Corrected from prior 9200-8E label. Est. range $30-50"),
    row("enc-ktnstl4", JBOD, "Enclosure", 1, "EMC KTN-STL4 15-bay JBOD",
        "15-bay 3U disk shelf, dual SAS/SATA backplane, SFF-8088 uplink", "EMC",
        "KTN-STL4", price="320", frm="FB Marketplace (Jake Meyer)",
        notes="Bundle price - includes 15x 2TB SAS drives + cables"),
    row("drv-boot-850evo", PN, "Drive-SSD;SATA", 1, "Samsung 850 EVO 250GB",
        "Proxmox OS + local-lvm (VM OS disks)", "Samsung", "850 EVO 250GB",
        serial="S21NNXAG319127V", notes="sdc. 25,431h", pool="boot / local-lvm"),
    row("drv-nvme-docker", PN, "Drive-NVMe", 1, "1TB NVMe SSD (Phison E13)",
        "Docker data-root + Ollama models", "Phison", "PS5013-E13 1TB",
        serial="20051410242634", notes="nvme0n1. 10,804h, 3% wear", pool="docker-nvme"),
    row("drv-apps-m600", PN, "Drive-SSD;SATA", 1, "Micron M600 512GB",
        "apps pool mirror member", "Micron", "MTFDDAY512MBF", serial="160614A66F74",
        notes="sde. 38,026h - aging, watch endurance", pool="apps", health="Watch"),
    row("drv-apps-satassd", PN, "Drive-SSD", 1, "SATA SSD 480GB",
        "apps pool mirror member", "(generic)", "SATA SSD", serial="19112048002378",
        notes="sdu. 1,971h - newest drive", pool="apps"),
    row("drv-logs-25", PN, "Drive-HDD;SAS", 2, "2.5in 500GB HDD (logs mirror)",
        "logs pool mirror, 2.5in laptop drives", "mixed",
        "HTS725050A7E630 / WD5000LPVX",
        notes="sdv HGST HTS725050A7E630 (TF655AWH0WJERL, CRC=9); sdw WDC WD5000LPVX (WD-WXA1A747U4EJ, CRC=2)",
        pool="logs", health="Watch - UDMA CRC errors, reseat/replace cables"),
    row("drv-sas-nm0043", JBOD, "Drive-HDD;SAS", 8, "Seagate Constellation ES.3 2TB SAS",
        "storage raidz2 members", "Seagate", "ST2000NM0043", frm="Bundled with KTN-STL4",
        notes="sdf(Z1X0BL7N000093373VP7) sdg(Z1X14EJ00000C410C3V2) sdi(Z1X64LP40000R620VHQ9) sdj(Z1X3VKC80000C520CT47) sdk(Z1X89XL70000C717BZHL) sdl(Z1X07KZ200009342P1BA) sdn(Z1X13C2W00009410NYKR) sdo(Z1X1H2090000C4225JCE)",
        pool="storage"),
    row("drv-sas-444ss", JBOD, "Drive-HDD;SAS", 2, "Seagate Constellation ES 2TB SAS",
        "storage raidz2 members", "Seagate", "ST32000444SS", frm="Bundled with KTN-STL4",
        notes="sdh(9WM8DKW40000C2429Y0J) sdm(9WM4E7390000C13375PU)", pool="storage",
        health="Watch - sdh has 1 grown-defect sector, replace at next chance"),
    row("drv-sas-hus723", JBOD, "Drive-HDD;SAS", 3, "HGST Ultrastar 7K3000 2TB SAS",
        "storage raidz2 members", "HGST", "HUS723020ALS64", frm="Bundled with KTN-STL4",
        notes="sdp(YGKY9P9K) sdq(YFKRZUDK) sdt(YGKY967K)", pool="storage"),
    row("drv-sas-hus724", JBOD, "Drive-HDD;SAS", 2, "HGST Ultrastar 7K4000 2TB SAS",
        "storage raidz2 members", "HGST", "HUS724020ALS61", frm="Bundled with KTN-STL4",
        notes="sdr(P6JV2H7V) sds(P5J5A1KV)", pool="storage"),
    row("drv-sata-dm008", PN, "Drive-HDD;SATA", 1, "Seagate Barracuda 2TB SATA",
        "storage raidz2-1 (internal, mixed vdev)", "Seagate", "ST2000DM008",
        serial="ZFL41V9E", notes="sda. 30,572h - aging consumer drive", pool="storage",
        health="Watch"),
    row("drv-sata-earx", PN, "Drive-HDD;SATA", 1, "WD 2TB SATA (EARX)",
        "storage raidz2-1 (internal, mixed vdev)", "Western Digital", "WD20EARX-22PASB0",
        serial="WD-WMAZA5966079",
        notes="sdb. 41,267h - OLDEST drive, legacy Green-class", pool="storage",
        health="Replace soon"),
    row("drv-sata-ezrz", PN, "Drive-HDD;SATA", 1, "WD Blue 2TB SATA",
        "storage raidz2-1 (internal, mixed vdev)", "Western Digital", "WD20EZRZ-00Z5HB0",
        serial="WD-WCC4N1SLUHXZ", notes="sdd. 25,178h", pool="storage"),
    row("spare-nvme500", SPARE, "Drive-NVMe", 1, "500GB NVMe (spare)",
        "Pulled from SLOG. Re-add as SLOG+L2ARC when 2nd M.2 slot re-enabled in BIOS",
        "(unknown)", "500GB NVMe", notes="Not installed - BIOS M.2 slot disabled",
        health="Spare"),
    row("spare-wd12tb", SPARE, "Drive-HDD;SATA", 1, "WD Ultrastar 12TB SATA",
        "Migration staging drive", "Western Digital", "Ultrastar 12TB", price="120",
        frm="FB Marketplace",
        notes="27K hours at purchase, SMART good. Not present in lsblk - location unconfirmed",
        health="Unknown"),
    row("wpc-cpu", WPC, "CPU", 1, "AMD Ryzen 5 3600",
        "Break-glass Proxmox spare / Zwift PC", "AMD", "3600", notes="6C/12T"),
    row("wpc-mb", WPC, "Motherboard", 1, "B450 motherboard", "Break-glass spare",
        "(unknown)", "B450", notes="AM4"),
    row("wpc-ram", WPC, "RAM", 1, "16GB DDR4", "Break-glass spare", "", "DDR4 16GB",
        notes="Capacity 16GB"),
    row("wpc-gpu", WPC, "GPU", 1, "AMD RX 5700 XT", "Break-glass spare / Zwift",
        "AMD", "RX 5700 XT"),

    # ── Gaming Rig (Office) ──────────────────────────────────────────────────
    row("gr-cpu", GR, "CPU", 1, "AMD Ryzen 9 9950X3D",
        "16C/32T Zen 5, AM5, 4.3GHz base / 5.7GHz boost, 3D V-Cache", "AMD",
        "9950X3D", price="556.48", frm=MC, time="2025-12-01",
        notes="Order 045-WP-10936157. 2yr protection plan purchased same order ($24.99)"),
    row("gr-mb", GR, "Motherboard", 1, "MSI X870E-P PRO WIFI",
        "AMD X870E, AM5, ATX, PCIe 5.0, WiFi 6E, 2.5GbE", "MSI",
        "X870E-P PRO WIFI", price="193.51", frm=MC, time="2025-12-01",
        notes="Order 045-WP-10936157"),
    row("gr-ram", GR, "RAM", 2, "Corsair Vengeance RGB 32GB DDR5",
        "2x32GB = 64GB total, DDR5-6000 CL30, PC5-48000, RGB", "Corsair",
        "CMH64GX5M2B6000C30", price="429.99", frm=MC, time="2025-12-01",
        notes="Order 045-WP-10936157. Sold as 64GB (2x32GB) kit"),
    row("gr-gpu", GR, "GPU", 1, "Gigabyte Radeon RX 7800 XT Gaming OC",
        "16GB GDDR6, RDNA 3, 2560 shaders, 256-bit, 19.5 TFLOPS, triple fan RGB",
        "Gigabyte", "GV-R78XTGAMING OC-16GD", price="424.96",
        frm="Micro Center", time="2023-12-12",
        notes="Order 045-PO-9953415. Clearance markdown CL0339593. Purchased Dec 2023, carried into Dec 2025 build"),
    row("gr-case", GR, "Case", 1, "Fractal Design Meshify 3 Ambience Pro RGB",
        "ATX Mid-Tower, tempered glass, 3x ARGB fans included", "Fractal Design",
        "Meshify 3 Ambience Pro RGB", price="114.96", frm=MC, time="2025-12-06",
        notes="Order 045-WP-10945378. Clearance markdown (CL0374661)"),
    row("gr-cooler", GR, "Cooler", 1, "ARCTIC Liquid Freezer III A-RGB 240mm",
        "240mm AIO liquid cooler, ARGB pump head + fans, AM5 compatible",
        "ARCTIC", "Liquid Freezer III A-RGB 240", price="124.99", frm=MC,
        time="2025-05-18", notes="Order 045-PO-10667120"),
    row("gr-ssd", GR, "Drive-NVMe", 1, "Samsung 990 EVO 1TB",
        "1TB NVMe, PCIe Gen 4x4 / Gen 5x2, up to 5,000MB/s read, TLC V-NAND",
        "Samsung", "MZ-V9E1T0BW", price="74.99", frm=MC, time="2025-07-26",
        notes="Order 045-PO-10758224"),
    row("gr-monitor", GR, "Monitor", 1, "AOC CU34G4 34\" Ultrawide",
        "34\" 2K WQHD 3440x1440, 180Hz, VA curved, 1ms MPRT, FreeSync Premium",
        "AOC", "CU34G4", price="249.99", frm=MC, time="2025-10-16",
        notes="Order 045-WP-10869056. 3yr protection plan purchased same order ($59.99)"),
    # ── Office / Laptop ──────────────────────────────────────────────────────
    row("office-laptop", "Office / Laptop", "Laptop", 1,
        "Lenovo ThinkBook 14 G2 ARE",
        "14\" laptop, AMD Ryzen 7 4700U (Zen 2, 8C/8T), 14\" FHD IPS, Grey",
        "Lenovo", "ThinkBook 14 G2 ARE", price="809.96", frm=MC, time="2021-07-16",
        notes="Order 045-PO-8786088. Clearance markdown CL0301805. 2yr accidental plan purchased same order ($269.99)"),

    row("gr-psu", GR, "PSU", 1, "Thermaltake Toughpower GT 1000W",
        "1000W, ATX 3.1, Native PCIe 5.1 12V-2x6, Full Modular, Flat Cables, 80+ Gold, 140mm fan",
        "Thermaltake", "PS-TPT-1000FNFAGU-3", price="99.99",
        frm="Amazon", time="2025-12-01",
        notes="Order 111-6404078-4816244"),
]

if __name__ == "__main__":
    out = "Docs/homebox-hardware-import.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(ITEMS)
    # Validate every row is exactly len(COLS)
    import csv as _c
    rows = list(_c.reader(open(out, newline="", encoding="utf-8")))
    bad = [i for i, r in enumerate(rows) if len(r) != len(COLS)]
    print(f"wrote {len(ITEMS)} items to {out}; field-count errors: {bad or 'none'}")

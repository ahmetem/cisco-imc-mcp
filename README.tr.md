# Cisco IMC MCP Sunucusu

Claude Desktop'ın (veya MCP destekli herhangi bir istemcinin) bir
**Cisco UCS C-Series** veya **HyperFlex** sunucusunu, Cisco Integrated
Management Controller (IMC) üzerinden, **XML API (Nuova)** ile yönetmesini
sağlayan yerel bir [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
sunucusu. Bu API tüm IMC firmware sürümlerinde (2.x, 3.x, 4.x) desteklenir.

**IMC firmware 4.1(2m)** çalıştıran bir **HX220C-M4S** ile test edilmiştir.

> 🇬🇧 English README: [README.md](./README.md)

## Özellikler

Üç kategoride on üç araç:

### Salt-okunur (otomatik çağrı güvenli)

| Araç | Açıklama |
|---|---|
| `imc_get_system_info` | Vendor, model, seri no, CPU/çekirdek, toplam RAM, BIOS POST durumu, IMC firmware sürümü |
| `imc_get_power_status` | Mevcut operasyonel güç durumu (on/off) ve BIOS POST durumu |
| `imc_get_health` | Güç kaynakları, fanlar, CPU sıcaklık sensörleri |
| `imc_get_psu_details` | PSU başına model, vendor, firmware, operability **artı input range, output voltage, max output** (doğrudan `equipmentPsu` native attribute'larından okunur) |
| `imc_list_drives` | Fiziksel diskler: slot, vendor, model, kapasite, tip, durum |
| `imc_get_disk_smart` | Disk başına sağlık ve hata sayaçları: `pdStatus`, `predictiveFailureCount`, `mediaErrorCount`, `otherErrorCount`, `linkSpeed`. Tek disk detayı için opsiyonel `slot_id`. |
| `imc_get_memory_health` | Tüm `memoryArray` container'ları boyunca DIMM başına presence, kapasite, hız, vendor, operability, operState |
| `imc_get_faults` | IMC'deki aktif fault instance'ları (`faultInst` MO) — XML API'de "Chassis → Faults" karşılığı. Opsiyonel `max_entries` (varsayılan 50) ve `severity_filter` (`critical`/`major`/`minor`/`warning`/`info`/`cleared`/`condition`). **SEL değil**; SEL Redfish API gerektirir ve bu sunucu henüz onu desteklemiyor. |

### Güç eylemleri (`confirm=true` gerektirir)

| Araç | Açıklama |
|---|---|
| `imc_power_on` | Sunucuyu aç (`adminPower=up`) |
| `imc_power_off_graceful` | ACPI üzerinden düzgün kapatma (`soft-shut-down`) |
| `imc_power_off_force` | Anında güç kes (`down`) — veri kaybına yol açabilir |
| `imc_reboot` | Zorla yeniden başlatma, hard reset (`hard-reset-immediate`) |
| `imc_power_cycle` | Güç çevrimi: önce kapat sonra aç (`cycle-immediate`) |

### Güvenlik

Eylem araçları `confirm=true` gerektirir. Ajan bu bayrağı açıkça
geçmek zorundadır — pratikte bu, Claude'un bu araçları yalnızca kullanıcı
eylemi açıkça istediğinde çağırması anlamına gelir. Salt-okunur araçlarda
böyle bir koruma yoktur.

`i_understand_data_loss=true` ikinci-bayrak deseni, ileride SEL'i
temizleyecek bir araç için saklı tutuluyor. Şu an bu MCP SEL'e yazamıyor
çünkü o işlem yalnızca Redfish API üzerinden çalışıyor, ve Redfish
desteği henüz eklenmedi.

## Gereksinimler

- **Python 3.11+**
- HTTPS üzerinden ulaşılabilen IMC'ye sahip bir Cisco UCS C-Series veya
  HyperFlex sunucu
- IMC kimlik bilgileri (envanter okuma ve güç kontrolü yetkisine sahip)
- Claude Desktop (veya herhangi bir MCP istemcisi)

## 1. Sunucuyu kur

### Windows (PowerShell)

```powershell
git clone https://github.com/<kullanici-adin>/cisco-imc-mcp.git C:\mcp-servers\cisco-imc-mcp
cd C:\mcp-servers\cisco-imc-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

PowerShell aktivasyon script'ini engelliyorsa, yönetici olarak açtığın bir
PowerShell'de bir kez şunu çalıştır:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Linux / macOS

```bash
git clone https://github.com/<kullanici-adin>/cisco-imc-mcp.git ~/mcp-servers/cisco-imc-mcp
cd ~/mcp-servers/cisco-imc-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. `.env` dosyasını yapılandır

```powershell
copy .env.example .env
notepad .env
```

Doldur:

```ini
IMC_HOST=192.168.1.106       # IMC web arayüzünün IP veya hostname'i
IMC_USERNAME=admin           # IMC kullanıcısı
IMC_PASSWORD=replace-me      # IMC parolası
IMC_VERIFY_SSL=false         # çoğu IMC self-signed sertifika kullanır
IMC_TIMEOUT=30
IMC_RACK_DN=sys/rack-unit-1  # varsayılan; nadiren değişir
```

`.env` dosyasını **asla** git'e commit etme. `.gitignore` zaten hariç tutar.

### `IMC_RACK_DN` hakkında

XML API, yönetilen nesnelere **Distinguished Name (DN)** ile erişir.
Standalone bir C-Series sunucu (ve çoğu HyperFlex tek-node) için rack-unit
DN'i `sys/rack-unit-1`'dir. Birden fazla rack-unit içeren bir kasan varsa
`sys/rack-unit-2` vb. olarak değiştirebilirsin.

## 3. Hızlı test

venv aktifken:

```powershell
python cisco_imc_mcp.py --help
```

Araç listesini görüp temiz çıkmalı. Burada bir import hatası alıyorsan bir
bağımlılık yüklenmemiştir.

Eğer projeyle birlikte `_diag.py` adlı yardımcı bir diyagnostik dosyası
varsa, bağlantıyı MCP protokolü dışında doğrulamak için onu da
çalıştırabilirsin:

```powershell
python _diag.py
```

## 4. Claude Desktop'a kaydet

Claude Desktop'ın config dosyasını aç:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Dosya yoksa oluştur. `mcpServers` bloğuna ekle (varsa genişlet):

```json
{
  "mcpServers": {
    "cisco-imc": {
      "command": "C:\\mcp-servers\\cisco-imc-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\mcp-servers\\cisco-imc-mcp\\cisco_imc_mcp.py"],
      "cwd": "C:\\mcp-servers\\cisco-imc-mcp"
    }
  }
}
```

Yolları sistemine göre ayarla. Windows'ta JSON içinde ters bölü iki katı
olmalı (`\\`).

Claude Desktop'ı tamamen kapat (tray ikonu → Çıkış) ve yeniden aç. Yeni bir
sohbette IMC araçları çekiç/connector ikonunda görünür.

## Sohbette ilk test

Salt-okunur bir çağrıyla başla:

> "Cisco sunucunun güç durumunu göster."

Claude `imc_get_power_status`'ı çağırır ve şuna benzer bir yanıt verir:
`Power state: on (BIOS POST: complete)`. Kimlik doğrulama hatası alırsan
`.env`'i tekrar kontrol et.

Sonra dene:

> "Sistem bilgisini göster."
>
> "Sunucu sağlık durumu nasıl? Fan veya PSU sorunu var mı?"
>
> "Fiziksel diskleri listele."

Salt-okunur araçların düzgün çalıştığından emin olduğunda eylemleri
deneyebilirsin:

> "Cisco sunucuyu yeniden başlat."

Claude onay isteyecek. Onayladıktan sonra `imc_reboot`'u `confirm=true` ile
çağırır.

## Örnek senaryolar

**Bakım öncesi kontrol:**

> "Cisco sunucunun sağlık raporunu ve güç durumunu ver."

Claude `imc_get_health` ve `imc_get_power_status`'ı çağırır.

**PSU 1 uyarısını araştır:**

> "PSU detaylarını input voltajıyla birlikte göster."

Claude `imc_get_psu_details`'ı çağırır; her PSU için V/A/W giriş değerleri
ve mevcut operability durumu döner.

**Sorunlu bir diske odaklan:**

> "Slot 3'ün SMART sayaçlarını göster."

Claude `imc_get_disk_smart`'ı `slot_id="3"` ile çağırır; `pdStatus`,
`predictiveFailureCount`, `mediaErrorCount`, `otherErrorCount` ve tüm
attribute dump'ını döner.

**ECC bellek hatalarını kontrol et:**

> "DIMM'lerden herhangi biri arızalı mı?"

Claude `imc_get_memory_health`'ı çağırır; operability/operState değeri
`operable`/`ok` olmayan slotları öne çıkarır.

**Aktif donanım fault'larını araştır:**

> "Cisco sunucuda şu an aktif olan donanım fault'ları neler?"

Claude `imc_get_faults`'u çağırır ve her aktif `faultInst`'ı severity,
kod, son geçiş zamanı ve insan-okunur açıklamasıyla listeler.

**Takılı sunucuyu power-cycle et:**

> "Cisco sunucu donmuş. Güç çevrimi uygula."

Onaydan sonra Claude `imc_power_cycle`'ı `confirm=true` ile çağırır.

**Disk envanteri:**

> "Cisco kutusunda kaç disk var ve durumları ne?"

Claude `imc_list_drives`'ı çağırır.

## Yapılandırma referansı

Tüm ayarlar `.env`'den okunan ortam değişkenlerinden gelir:

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `IMC_HOST` | — (zorunlu) | IMC web UI'nin IP veya hostname'i |
| `IMC_USERNAME` | — (zorunlu) | IMC kullanıcı adı |
| `IMC_PASSWORD` | — (zorunlu) | IMC parolası |
| `IMC_VERIFY_SSL` | `false` | IMC'nin TLS sertifikasını doğrula |
| `IMC_TIMEOUT` | `30` | HTTP timeout (saniye) |
| `IMC_RACK_DN` | `sys/rack-unit-1` | Yönetilecek rack-unit'in DN'i |

## Nasıl çalışır (XML API)

Bu sunucu, Redfish yerine Cisco'nun daha eski ama evrensel olarak
desteklenen **Nuova XML API**'sini (POST → `/nuova`) kullanır. Sebebi:
Redfish, eski IMC firmware'lerinde (özellikle HX/M4 donanımda) tam olarak
implement edilmemiştir; XML API ise IMC'nin ilk sürümlerinden beri
stabildir.

Her araç çağrısının session yaşam döngüsü:

1. `aaaLogin` — session cookie al
2. Bir veya daha fazla `configResolveDn`, `configResolveClass`,
   `configConfMo`
3. `aaaLogout` — cookie'yi serbest bırak (hata olsa bile her zaman)

Güç eylemleri için sunucu, `computeRackUnit` nesnesi üzerinde `adminPower`
özelliğini yazar. IMC bunu alıp operasyonel durumu sürer.

## Güvenlik notları

- IMC parolası `.env`'de duruyor. Bu dosyayı yalnızca kendi kullanıcına
  okunabilir yap (Windows'ta `icacls`, Linux'ta `chmod 600`).
- IMC'yi asla internete açma. Güvenilir bir LAN/VLAN'da veya VPN arkasında
  tut.
- Mümkünse `admin` yerine ayrı bir IMC kullanıcısı kullan.
- Eylem araçları `confirm=true` gerektirir. Bu korumayı kaldırma.
- Varsayılan `IMC_VERIFY_SSL=false`, çünkü çoğu IMC self-signed sertifika
  kullanır. Güvenilir bir sertifika kurduysan `true` yap.

## Sorun giderme

- **"Cannot connect to IMC at <host>"**
  Ağ sorunu. IMC'ye ping at. Aynı makineden `https://<host>/` adresinin
  açıldığını doğrula.

- **"IMC login returned no cookie (auth failed?)"**
  Kullanıcı adı veya parola yanlış, ya da kullanıcı kilitli. Web UI'ya
  aynı kimlik bilgileriyle girmeyi dene.

- **"IMC API error <n>: <açıklama>"**
  XML API hata döndü. Açıklama genellikle sorunu söyler. En sık olanlar:
  permission denied (kullanıcının rolü eksik), yanlış DN (`IMC_RACK_DN`
  senin kasan için yanlış).

- **"Resource not found"** veya boş sonuçlar
  Çok eski IMC firmware'leri farklı class ID'leri kullanabilir. Bu
  sunucunun okuduğu MO'lar — `computeRackUnit`, `equipmentPsu`,
  `equipmentFan`, `processorEnvStats`, `storageLocalDisk`, `memoryArray`,
  `memoryUnit`, `faultInst` — IMC 2.x'ten beri stabildir ama nadir
  varyantlar olabilir.

- **Eylemden hemen sonra güç durumu değişmiyor.**
  Normal. Cisco donanımı güç geçişlerinin oturması için 30-90 saniye
  alır. Bekleyip `imc_get_power_status`'ı tekrar çağır.

- **Araçlar Claude Desktop'ta görünmüyor.**
  Hatalar için `%APPDATA%\Claude\logs\mcp*.log` (Windows) loglarına bak.
  En sık neden `claude_desktop_config.json`'da yanlış yol veya
  çiftlenmemiş ters bölü.

## Proje yapısı

```
cisco-imc-mcp/
├── cisco_imc_mcp.py    # MCP sunucusu
├── _diag.py            # Opsiyonel bağlantı diyagnostiği
├── requirements.txt    # Python bağımlılıkları
├── .env.example        # Yerel .env için şablon
├── .gitignore
├── LICENSE             # GPL v3
├── README.md           # İngilizce sürüm
└── README.tr.md        # Bu dosya
```

## Katkı

Issue ve PR'lara açık. Bir araç eklersen lütfen:

1. Mevcut deseni takip et: pydantic input modeli + `_require_config` +
   `_imc_session` context manager + hata yönetimi.
2. Yıkıcı araçları annotations'ta `destructiveHint: True` ile işaretle ve
   input modelinde `confirm=True` zorunlu kıl.
3. Bu README'deki araç listesini güncelle.

## Lisans

[GNU General Public License v3.0](./LICENSE) — tam metin için `LICENSE`
dosyasına bak.
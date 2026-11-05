# Иконки актёров (логотипы)

Оригиналы Стаса на рабочем столе лежат как `01.jpg` … `08.jpg` - по порядку актёров.
В проекте копии: `assets/icons/<номер>_<актёр>.jpg`.

**Правило: иконки живут ТОЛЬКО в `assets/icons/`. Внутрь `actors/<slug>/` их не кладём -
иначе при `apify push` картинка уезжает на платформу как файл исходников.**

| № | файл | актёр (slug в сторе) | actorId | цена | md5 | размер |
|---|------|----------------------|---------|------|-----|--------|
| 01 | `01_website-screenshot.jpg` | `stas_k/website-screenshot-visual-diff` | `KJlZ1w1PjP59l4usw` | $0.05 / снимок | `6cd7b0abded1be7ce5b8a5c6392c5e89` | 354 102 |
| 02 | `02_youtube-transcript.jpg` | `stas_k/youtube-transcript-api-fast` | `GuanyXsDjWKCwRK45` | $0.003 / видео | `c9c45295537eb88a8bd3adb39431c667` | 94 664 |
| 03 | `03_company-jobs.jpg` | `stas_k/company-jobs-api` | `JfScsAVmXb5VnofgJ` | $0.0015 / вакансия | `f39b55c8afe8a360875ec48df289d4bb` | 359 763 |
| 04 | `04_website-contacts.jpg` | `stas_k/website-contacts-finder` | `uhUG2WtA670A2gqNX` | $0.02 / сайт с контактами | `08676871035c15cfb6def088b60872cd` | 397 218 |
| 05 | `05_workday-jobs.jpg` | `stas_k/workday-jobs-api` | `NdJWWljtR7mfDrPEj` | $0.0012 / вакансия | `dc5460f1a0a0a8faa12c90728165e5d3` | 417 291 |
| 06 | `06_iban-validator.jpg` | `stas_k/iban-validator` | `Sm7LuEa8ulgbkrws3` | $0.001 / IBAN | `7ddb4f3b6f49740341b8bad6b8291444` | 453 156 |
| 07 | `07_vat-validator.jpg` | `stas_k/vat-validator` | `fKtLkgGNziei2zdIt` | $0.001 / номер | `5ac3f75137d0e390cdfc8317afcc05dc` | 435 408 |
| 08 | `08_ssl-certificate-checker.jpg` | `stas_k/ssl-certificate-checker` | `waaDKYGljTycbXqsr` | $0.001 / хост | `839d100536a5532d678607db2ec23190` | 451 710 |

Проверка, что на витрине именно наш файл, - по `pictureUrl` через API актёра
(`GET /v2/acts/<id>`); у неопубликованного актёра поля нет вовсе, смотреть в консоли.

## Как ставится иконка

API картинку не принимает (`invalid-picture-url`), поэтому только консоль:
Publishing → Display information → Icon → **Upload new image** → кроп «Looks good» → **Save**.
После Save обязательно перезагрузить страницу: незасохранённая загрузка висит превью
и создаёт «You have unsaved changes from last time» - на этом я уже попадалась.

ВНИМАНИЕ: кнопка формы называется просто «Save», а в шапке актёра сидит
«Save as a new task» - клик по тексту «Save» попадает в шапку и открывает
диалог создания задачи. Жать Save в форме по метке/рефу, а не по тексту.

Платформа пережимает картинку: исходник 350-420 КБ превращается в 20-40 КБ. Это норма.

## Промпт для нового логотипа

Первой строкой всегда: `тот же стиль, что и у остальных` (Стас генерирует все иконки
в одном окне Gemini-чата). Дальше - один блок описания, без «короткой версии на случай каши».
Требования: 512×512 PNG, без букв и цифр, силуэт читается на 64 px в карточке стора.

## _legacy/

`assets/icons/_legacy/` - старые имена тех же файлов (`actor_icon.jpg`,
`website_contacts_icon.jpg`, …) и первый `logo.png`, который я делала сама и держала
на нашем VPS. Не нужны для работы, но пусть лежат: вдруг понадобится старый адрес.

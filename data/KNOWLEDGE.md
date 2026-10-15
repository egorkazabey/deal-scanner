# Знания перекупа (для сканера и для Gemini)

Регион: Ústí nad Orlicí (PSČ 56201), радиус 60 км. Также Pardubice (53002), Hradec Králové, Svitavy, Chrudim, Česká Třebová, Vysoké Mýto, Litomyšl, Lanškroun, Žamberk, Choceň, Letohrad, Rychnov n. Kn., Holice, Polička, Králíky, Náchod, Šumperk, Přelouč, Trutnov (край радиуса).

## Правило покупки
- Цель: купить за ≤ 72 % от `market_price` (рыночная цена, за которую вещь реально уходит).
- Первое предложение на 5–10 % ниже цели. Если продавец пишет «cena pevná», то максимум цель + 3 %.
- Минимальная прибыль, ради которой стоит ехать: телефоны и часы от 1 000 Kč, консоли от 1 000 Kč, ПК и видеокарты от 2 000 Kč.
- Если ликвидность 1, нужен запас больше: покупать за ≤ 68 %.

## Корректировки рыночной цены по состоянию
- Батарея iPhone < 80 %: −8–10 %. Батарея 100 % после замены: смотреть, оригинальная ли (История деталей).
- Заменённый дисплей или неоригинальные детали в «История деталей и сервиса»: −10–15 %.
- Разбитая задняя крышка iPhone 15/16: −20–25 % (ремонт ~2–3k).
- Трещина на дисплее у iPad Pro/OLED: не брать (ремонт дороже выгоды).
- Царапины на стекле Apple Watch: −15–20 %.
- Нет коробки/зарядки: −3–5 %. Полный комплект + чек: +3–5 %.
- Дрифт Joy-Con: −300–500 Kč на стик.
- Ещё на гарантии с чеком: +5 %.

## Шаблоны мошенников (помечать красным, не предлагать)
- FB: iPhone 15/16 Pro Max за 6 100–8 700 со «странными» ценами (8 3xx, 8 5xx, 8 6xx) и пачками одинаковых объявлений.
- Bazoš: Jeseník — PS5 Pro за 8–9k, Switch 2 за 4 500, iPhone 14 Pro за 7 500.
- Bazoš: «Xbox Series X» за 4 500–6 000 (Chrudim и др.) с текстом «pošlete zprávu na e-mail» — шаблон развода.
- Тройки одинаковых объявлений (например Jablonec 15 Pro).
- Цена < 55 % от рынка + «jen e-mail», «pouze zaslání», «platba předem», WhatsApp-only у новых аккаунтов, ссылки на «доставку».
- Продажа «po dceři/synovi» сама по себе НЕ красный флаг.

## Нормальные красные флаги (снижают оценку, но не мошенничество)
- «Měl by někdo zájem o výměnu» — обмен, а не продажа.
- Сервисы/магазины (záruka 12M, IČO, «provozovna») — цены розничные, маржи нет.
- «Prodáno» в заголовке.
- iCloud/aktivační zámek, «blokovaný», «na díly», MDM.

## Что проверять при встрече (кратко в уведомлении не нужно, но Gemini может напоминать специфичное)
- iPhone: Find My выключен, сброс до «Hello» без чужого Apple ID, IMEI на checkcoverage.apple.com, История деталей, батарея, Face ID, камеры, MDM.
- Watch: отвязаны от iPhone продавца, батарея, стекло.
- Switch: eShop без ошибки 2124-4508 (бан), нет CFW (hekate/Atmosphère), дрифт Joy-Con, выгорание OLED, док/HDMI, оригинальный блок 15 V.
- ПК: бенчмарк/стресс 10 мин, температуры, модели материнки и блока питания (для 3080 ≥ 750 W нормального бренда), RAM в двухканале, майнинг.
- Видеокарта: стресс-тест, температуры, вентиляторы, пломбы, не из майнинга.
- Всегда: деньги только после проверки, kupní smlouva.

## Оценка готовых ПК
Сумма б/у комплектующих × 0.8 = за сколько уходит ПК целиком. Ориентиры (Kč, 10/2026):
- CPU: i5-10400F 1 500, i5-12400F 2 500, i7-11700F 2 700, i5-12600KF 3 300, R5 3600 1 300, R5 5500 1 600, R5 5600 2 000, R7 5700X 3 000.
- RAM DDR4 16 GB 900–1 200, 32 GB 1 800–2 400, 64 GB 3 000–4 500.
- SSD NVMe 1 TB 900–1 200, 2 TB 1 800–2 500.
- Материнка 1 200–2 500, блок питания 700–1 500 (noname 300), корпус+кулер 800–1 500.
- OEM-сборки HP/Dell/Lenovo с проприетарными платами и блоками: −15 %.

## Сообщения продавцам (чешский, шаблоны)
- Первое: «Dobrý den, je [věc] ještě k dispozici? Mohl bych přijet [dnes/zítra]. Šlo by to za [nabídka] Kč při osobním předání? Děkuji.»
- iPhone: добавить «Byl telefon někdy v servisu (díly, baterie)? Jaká je kondice baterie?»
- ПК: «Mohl byste napsat přesný model základní desky, zdroje a grafiky? Byla karta použitá na těžbu?»
- Если «cena pevná»: «Rozumím. Mohl bych přijet hned a zaplatit hotově – šlo by [cíl] Kč?»

## Техника парсинга (проверено вручную в Chrome, 10/2026)

### Bazoš
- Поиск: `https://{mobil|pc}.bazos.cz/?hledat=QUERY&rubriky={mobil|pc}&hlokalita=56201&humkreis=60&cenaod=MIN&cenado=MAX`
- Пагинация: `https://mobil.bazos.cz/20/?hledat=...` (по 20 штук).
- Категории: `https://pc.bazos.cz/graficka/?hlokalita=56201&humkreis=60&cenaod=..&cenado=..`, мониторы `pc.bazos.cz/monitor/`.
- Карточка в выдаче: `.inzeraty`; заголовок и ссылка `.nadpis a` (href содержит `/inzerat/ID/slug.php`); цена `.inzeratycena`; локация `.inzeratylok` (первая строка = город, вторая = PSČ); дата в тексте `[15.10. 2026]`.
- Деталь: нужен ПОЛНЫЙ href со slug (`/inzerat/ID/` без slug открывает не то). `h1` = заголовок, цена через regex `Cena:\s*([^\n]+)`, описание `.popisdetail`, дата `\[\d+\.\d+\. \d{4}\]`.
- В выдаче бывают TOP-объявления, они не по дате. Дедупликация по ID.
- Уточнения (проверено парсером 15.10.2026):
  - Карточка = `div.inzeraty` (класс `inzeraty inzeratyflex`); строка-заголовок таблицы — `div.listainzerat`, её селектор не цепляет.
  - TOP-метка: `span.ztop` внутри `.velikost10` (там же дата: ` - TOP - [15.10. 2026]`).
  - Локация: `Svitavy<br>568 02`.
  - Пагинация: блок `.strankovani`, следующая страница есть, если там есть ссылка «Další». Для категорий путь `/graficka/20/?...`.
  - Деталь: заголовок `<H1 class=nadpisdetail>`; «Cena:» стоит в `<td>`, а значение в соседнем `<td>`, поэтому regex берём по тексту body с разделителем `\n` (в `<meta description>` тоже есть «Cena:», его не трогаем).
  - Радиус 60 км по PSČ захватывает и Jeseník, Olomouc, Blansko, Žďár n. S.; в выдаче бывают объявления 2-месячной давности.
  - Поиск по тексту находит и мусор (аксессуары, инструменты, где слово есть в описании); отсеивать будет pricing.
- Запросы (рубрика mobil): iphone, apple watch, airpods, ipad. Запросы (рубрика pc): macbook, playstation, xbox, nintendo switch, herní pc, rtx, + категория graficka.

### Facebook Marketplace
- Нужен залогиненный браузер (Playwright persistent profile). Без логина выдача пустая или со стеной входа.
- Поиск: `https://www.facebook.com/marketplace/search?query=Q&minPrice=..&maxPrice=..&daysSinceListed=1&exact=false`. FB перенаправляет на `/marketplace/<locationId>/search/...`; локация и радиус (60 км) берутся из настроек аккаунта.
- Карточки: `a[href*="/marketplace/item/"]`, ID из `item/(\d+)`. `innerText` по строкам: строка с `CZK` = цена (может быть две цены — новая и старая, брать первую), первая другая строка = заголовок, вторая = город. Строку «Недавно опубликовано» / «Just listed» пропускать.
- Цены парсить regex `(\d[\d\s  ]*)\s*CZK`.
- Выдача по запросу ~20–30 штук, догружается прокруткой (4–5 прокруток с паузой 1.5 с).
- Деталь: `https://www.facebook.com/marketplace/item/ID/` — текст в `[role=main]` или `div[role=dialog]`; описание до блока «Информация о продавце»/«Seller information». Продано = «Продано»/«Sold»; удалено = редирект с `unavailable_product`.
- Язык интерфейса влияет на тексты (у меня русский). Делать парсер независимым от языка где можно.

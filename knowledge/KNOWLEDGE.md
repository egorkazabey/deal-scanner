# Знания перекупа (для сканера и для Gemini)

Регион: Ústí nad Orlicí (PSČ 56201), радиус 60 км. Также Pardubice (53002), Hradec Králové, Svitavy, Chrudim, Česká Třebová, Vysoké Mýto, Litomyšl, Lanškroun, Žamberk, Choceň, Letohrad, Rychnov n. Kn., Holice, Polička, Králíky, Náchod, Šumperk, Přelouč, Trutnov (край радиуса).

## Правило покупки
`market` = `market_price` (рыночная цена, за которую вещь реально уходит), с поправками на состояние.
Потолок покупки `max_price` ступенчатый (`pricing.max_price_tiers` в config.yaml, строки ниже должны совпадать с ним):
  - market < 5 000: max_price = min(market × 0.70, market − 1 000)
  - market 5 000–12 000: max_price = min(market × 0.78, market − 1 200)
  - market > 12 000: max_price = min(market × 0.85, market − 2 000)
  - fashion: любой market: max_price = min(market × 0.65, market − 400)
  - gpu/macbook/pc: минимальная прибыль (market − max_price) не меньше 2 000 на всех ступенях
  - liquidity = 1: коэффициент ещё −0.04
  - max_price округлить вниз до 100
  - extra_cost (Vinted: ochrana kupujících + doprava) платится сверху: max_price для цены объявления = max_price − extra_cost (вниз до 100), прибыль = market − цена − extra_cost
  - offer_price = min(0.92 × цена объявления, max_price − 5 %), округлить вниз до 100
- Пример: Apple Watch Ultra 3, market 13 800, объявление 11 999 → max_price 11 700, offer_price 11 000.
- Кандидат только при ожидаемой прибыли (market − цена) от 1 000 Kč, для ПК, видеокарт и MacBook от 2 000 Kč.

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
- Vinted: просьба оплатить мимо Vinted (převod, PayPal friends, Revolut), уйти в WhatsApp/Telegram/e-mail,
  «pošlu jinou dopravou», ссылка на «свою» страницу оплаты/доставки — развод (scam_risk high). Покупка только
  через кнопку Vinted (там защита покупателя). Флагманы из-за рубежа за 20–35 % рынка (iPhone 16 Pro 512 GB
  за 3 000 Kč) — почти всегда развод или iCloud-лок. Правила: у зарубежного продавца цена < 55 % рынка =
  too_cheap (`pricing.foreign_min_price_pct`), у чешского как обычно < 40 %.
- Vinted: AirPods (особенно Pro 3 / Max) из-за рубежа по одинаковой цене у разных продавцов — почти всегда
  подделки. Без фото серийного номера / чека scam_risk не ниже medium.

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

## Одежда и кроссовки (Vinted)
Прайс: `knowledge/fashion.json`, категория `fashion`. market_price = цена б/у в состоянии «Velmi dobrý» и
ходового размера. Правила (scanner/fashion.py) уже поправили market по состоянию Vinted (Nový s visačkou 125 %,
Nový bez visačky 110 %, Velmi dobrý 100 %, Dobrý 80 %, Uspokojivý 50 %) и снизили ликвидность для редких размеров
(кроссовки вне EU 40–46, одежда не S–XL). Детские размеры обуви (< 36) не берём.
- Главный риск — подделки. Признаки: цена < 50 % рынка у «новых» вещей, стоковые фото из интернета, нет фото
  бирок / этикетки с кодом (у Nike: стиль-код вида DD1391-100 на бирке в ботинке), «bez krabice» у новых,
  продавец с кучей одинаковых пар разных размеров, слова «replika», «1:1», «UA», «kopie», «podróbka».
  Moncler, Canada Goose, Stone Island, Yeezy, Jordan 4 подделывают чаще всего: без фото бирок и QR/NFC-кода
  scam_risk не ниже medium.
- Поправки к market сверх состояния: пятна, дырки, стёртая подошва, пожелтевшая подошва у белых кроссовок
  −15–30 %; без коробки у кроссовок −5 %; полный комплект (коробка, бирки, чек) +5 %.
- Стили и размеры: размер в объявлении EU; у кроссовок спросить длину стельки в см, если размер под вопросом.
- Сообщение продавцу (чешский, Vinted): «Dobrý den, jsou boty originální? Můžete prosím poslat fotku štítku
  uvnitř? Šlo by to za [nabídka] Kč?» Для одежды вместо «boty» — «bunda/mikina», вопрос про бирку и размеры (cm).

## Оценка готовых ПК
Считает код (scanner/pc.py), цены компонентов в `knowledge/components.json`, видеокарты из `prices.json`.
- Gemini только читает железо из заголовка и описания (CPU, GPU, RAM, диски, блок питания, материнка, монитор, OEM).
- Стоимость ПК = сумма б/у цен компонентов × коэффициент. OEM-сборка (HP/Dell/Lenovo/Acer, проприетарные платы и блоки): ещё −15 %.
- Коэффициент по классу железа:
  - ×1.0: GPU уровня RTX 3060 / RX 6600 и выше (по цене в прайсе не дешевле RX 6600) И CPU от Intel 10 gen / Ryzen 3000;
  - ×0.8: DDR3, или GTX 10xx и слабее (RX 4xx/5xx), или CPU до Intel 8 gen;
  - ×0.9: всё остальное (обычно DDR4 со средним железом, Intel 8–9 gen, Ryzen 1000–2000).
- Монитор в комплекте: отдельной строкой по полной цене, добавляется к стоимости.
- Неизвестная модель CPU: ближайшая по поколению из таблицы; неизвестный объём RAM/SSD или монитор: ближайший. Такая оценка неточная (low confidence).
- Блок питания: брендовый 650 W+ 1 100, noname 300; брендовый слабее 650 W и неописанный считаются как noname.
- Не определены CPU или GPU: ПК не оценивается, флаг «нет данных о железе».
- Флаги: noname блок питания при видеокарте уровня RTX 3070+; один модуль RAM = «одноканал».
- Минимальная прибыль 2 000, потолок по ступеням как у остальных (обычно ступени 5 000–12 000 и > 12 000), ликвидность 2.
- Пример: R5 5600X / RTX 2080 / 16 GB DDR4 / 1 TB / EVGA 750W: сумма 13 700 × 1.0 = 13 700 → max 11 600.

## Сообщения продавцам (чешский, шаблоны)
- Первое: «Dobrý den, je [věc] ještě k dispozici? Mohl bych přijet [dnes/zítra]. Šlo by to za [nabídka] Kč při osobním předání? Děkuji.»
- iPhone: добавить «Byl telefon někdy v servisu (díly, baterie)? Jaká je kondice baterie?»
- ПК: «Mohl byste napsat přesný model základní desky, zdroje a grafiky? Byla karta použitá na těžbu?»
- Если «cena pevná»: «Rozumím. Mohl bych přijet hned a zaplatit hotově – šlo by [cíl] Kč?»
- Vinted: личной встречи нет, цену предлагают кнопкой «Nabídnout cenu». Сообщение: «Dobrý den, [вопрос про
  состояние / батарею]. Šlo by to za [nabídka] Kč?» Без «osobní předání» и «přijet». Иностранному продавцу
  тоже по-чешски, коротко и просто.

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

### Sbazar
- Сайт на Astro. Выдача лежит в атрибуте `props` тега `<astro-island>`, у которого component-url содержит `SearchPageList` (JSON, HTML-экранированный). Сериализация Astro: `[0, x]` = значение, `[1, [...]]` = массив, всё вложено так же; `props.offers` = `{pagination: {limit: 58, offset, total}, results: [...]}`.
- Поля объявления: `id` (int), `name`, `price`, `priceByAgreement`, `seoName` (`235124844-apple-iphone-16-plus-...`), `createDate` (дата подачи, берём её), `sortingDate`/`editDate` (поднятие/правка), `topped`, `locality` {`municipality`, `district`, `zip`}. Описания в выдаче нет. Объявления с id-строкой и `locality` = null это реклама магазинов Zboží.cz, пропускаем.
- Поиск: `https://www.sbazar.cz/hledej/{запрос}/0-vsechny-kategorie/usti-nad-orlici/cena-od-{min}-do-{max}-kc?radius=30`, страница 2: `.../cena-od-...-kc/nejnovejsi/2?radius=30`. Новые идут первыми (топ-объявления в начале). Радиус считается от района Ústí nad Orlicí: 30 км ≈ Bazoš 60 км от PSČ (Chrudim, Hradec Králové, Náchod, Olomouc, Pardubice, Rychnov, Svitavy, Šumperk, Trutnov, Žďár); 60 км уже Jihlava и Kolín.
- Рубрики «graficka» на Sbazar нет, ищем текстом `graficka karta` (`sbazar.queries` в config.yaml).
- Деталь: `https://www.sbazar.cz/inzerat/{seoName}`, в `<script type="application/ld+json">` объект `@type: Product` с `name`, `description` (HTML-сущности вроде `&quot;`), `offers.price`. Удалённое объявление отвечает 404.
- Заголовок `Accept: text/html` уводит на автологин Seznam и стену согласия (cmp.seznam.cz). Шлём `Accept: */*` и редиректы за пределы sbazar.cz считаем ошибкой.

### Vinted
Проверено живыми запросами 28.10.2026 (без логина, антибота не было).
- Поиск (HTML, Next.js): `https://www.vinted.cz/catalog?search_text=Q&catalog[]=ID&price_from=..&price_to=..&currency=CZK&order=newest_first&page=N`, 96 объявлений на страницу, максимум 10 страниц. Старый JSON API `/api/v2/catalog/items` отвечает 404.
- Данные лежат в скриптах `self.__next_f.push([1, "..."])`. Склеить строки, найти `"items":{"items":[...]`, рядом `"pagination":{"current_page","total_pages",...}`.
- Объявление: `productItem` = `id`, `title`, `url` (`/items/{id}-slug`), `price.amount` (строка, `"2964.74"`), `serviceFee` (ochrana kupujících = 5 % + 18 Kč), `totalItemPrice`, `isPromoted`, `user.isBusiness`, `itemBox.accessibilityLabel` = «…, Značka: X, Model: Y, Stav: Z, Velikost: N, цена, итог». Даты подачи и города в выдаче нет: дата = день, когда бот впервые увидел объявление.
- Заграница: Vinted.cz подмешивает продавцов из PL/SK/HU/RO (~90 % выдачи), их цены пересчитаны в Kč с копейками. Целая цена (`13500.00`) = чешский продавец. Доставка в расчёте: `vinted.shipping_czk` (ČR) / `shipping_abroad_czk` (из-за рубежа) в config.yaml.
- Категории (`catalog[]`): 3661 мобильные телефоны, 3035 смарт-часы, 3678 наушники, 3728 планшеты, 3580 ноутбуки, 3025 консоли, 3581 настольные ПК, 3602 видеокарты. Без категории поиск «iphone» приносит чехлы.
- Состояние (Stav): Nový s visačkou, Nový bez visačky, Velmi dobrý, Dobrý, Uspokojivý.
- Деталь: `https://www.vinted.cz/items/{id}-slug`, `<script type="application/ld+json">` Product: `name`, `description`, `offers.price`, `offers.availability` (`InStock`; другое = продано). Во flight-данных есть блоки `attributes` (бренд, модель, память, состояние), `summary` («Vystaveno před 28 minutami»), `user_info_header` (отзывы продавца). Несуществующее объявление отвечает 404.
- Заголовок `Accept: text/html` работает. Сессия: сначала главная страница (cookie `access_token_web`, `anon_id`), потом поиск. Антибот (DataDome/Cloudflare, 403, редирект на captcha) не обходим: источник останавливается, бот присылает кнопку «Проверить Vinted».

### Facebook Marketplace
- Нужен залогиненный браузер (Playwright persistent profile). Без логина выдача пустая или со стеной входа.
- Поиск: `https://www.facebook.com/marketplace/search?query=Q&minPrice=..&maxPrice=..&daysSinceListed=1&exact=false`. FB перенаправляет на `/marketplace/<locationId>/search/...`; локация и радиус (60 км) берутся из настроек аккаунта.
- Карточки: `a[href*="/marketplace/item/"]`, ID из `item/(\d+)`. `innerText` по строкам: строка с `CZK` = цена (может быть две цены — новая и старая, брать первую), первая другая строка = заголовок, вторая = город. Строку «Недавно опубликовано» / «Just listed» пропускать.
- Цены парсить regex `(\d[\d\s  ]*)\s*CZK`.
- Выдача по запросу ~20–30 штук, догружается прокруткой (4–5 прокруток с паузой 1.5 с).
- Деталь: `https://www.facebook.com/marketplace/item/ID/` — текст в `[role=main]` или `div[role=dialog]`; описание до блока «Информация о продавце»/«Seller information». Продано = «Продано»/«Sold»; удалено = редирект с `unavailable_product`.
- Язык интерфейса влияет на тексты (у меня русский). Делать парсер независимым от языка где можно.
- Реализация (scanner/sources/facebook.py, 10/2026): строка цены = только число и `CZK`/`Kč` (`12 500 CZK`,
  `CZK 12,500`, `9 990 Kč`); первая такая строка = цена, вторая (старая) пропускается. Служебные строки
  «Недавно опубликовано» / «Just listed» / «Nově přidáno» / «Бесплатно» / «Free» / «Zdarma» отбрасываются.
  Город = строка после заголовка до запятой; объявления не из списка городов (home/near_cities) выкидываются.
- Стена входа: URL с `/login`, `/checkpoint`, `/two_step_verification` или форма входа на странице. Не обходить:
  источник останавливается, бот просит `fb-login`.
- Живой FB парсером ещё не проверялся (из облачной среды facebook.com недоступен): первый прогон
  `run-once --source fb --dry` сверить и при расхождениях поправить этот раздел.

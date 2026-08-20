#!/usr/bin/env python3
"""Действующие зоны из АИП России — в формат OpenAir, который читает приложение.

Пилот сказал прямо: файл планеристов 2016 года стар, надо брать действующее.
Действующее лежит в открытом доступе: ЦАИ ГА публикует АИП разделами в PDF,
и раздел ENR 5.1 — это запретные зоны, зоны ограничения полётов и опасные,
с координатами прямо в тексте.

    tools/aip_zones.py [--pdf enr5.1.pdf] [--out russia-aip.txt] [--fir UL]

Без `--pdf` качает свежий раздел с сайта ЦАИ ГА. `--fir` оставляет зоны
одного района (UL — Санкт-Петербург и Северо-Запад).

**Что это не отменяет.** Файл — пересказ документа, а не сам документ.
Разбор терпимый: зона, которую не удалось прочесть, не выдумывается,
а пропускается со счётчиком. Ошибка в разборе не видна в воздухе, поэтому
приложение и показывает дату АИП рядом с зонами и советует сверяться
с первоисточником.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

AIP_BASE = "https://www.caica.ru/common/AirInter/validaip/aip"

# Что откуда берётся.
#
# ENR 5.1 — запретные зоны, зоны ограничения полётов и опасные: то, куда
# нельзя. Разделы 5.2 и 5.3 отсылают к нему же, 5.4 (препятствия) и 5.5
# (спортивные полёты) в АИП не публикуются вовсе — проверено.
#
# ENR 2.1 и 2.2 — диспетчерские районы и секторы. **Разбор написан,
# но его вывод пользоваться нельзя, и вот почему.**
#
# Таблица этого раздела в PDF устроена так, что надёжно разрезать её
# на зоны не выходит: заголовок опознаётся по паре строк «русская —
# английская», а в них смешаны алфавиты (латинская «C» в русском слове,
# кириллическая «А» в английском), колонка примечаний переносится
# и разрывает пару, часть зон описана ссылками «в границах секторов А2,
# ШД1», часть — дырками «исключая район, ограниченный координатами».
#
# Две проверки подряд дали одно и то же: контуры склеиваются с соседними.
# Сектор Сыктывкара накрывал Санкт-Петербург, аэродром Пулково оказывался
# **вне** своей же диспетчерской зоны, у секторов без координат появлялись
# чужие границы. Каждая правка чинила одно и открывала следующее — признак
# того, что беда в подходе, а не в частностях.
#
# Поэтому раздел оставлен в инструменте, но в файл зон не идёт: ложная
# граница на карте хуже её отсутствия, потому что по ней принимают решения.
# Чтобы это сделать честно, нужен машиночитаемый источник (AIXM), а не PDF.
SECTIONS = {
    "enr5": ["enr/enr5/enr5.1.pdf"],
    "enr2": ["enr/enr2/enr2.1.pdf", "enr/enr2/enr2.2.pdf"],
}

# ULP3, UNR50, UHD90 — район, тип, номер. Иногда с буквой: ULR12A.
ZONE = re.compile(r"^\s*(U[A-Z]{1}[PRD]\d+[A-Z]?)\s*$", re.M)

# 591828N 0280906E, а на Чукотке — 663000N 1753450W.
#
# Полушарие обязательно: без него тринадцать зон Чукотского АДЦ, записанных
# западной долготой, выпадали целиком. За сто восьмидесятым меридианом
# у России кончается не страна, а знак долготы.
# Буквы полушария — классами: в документе 149 координат записаны
# с кириллической «Е» (U+0415) вместо латинской «E» (U+0045). На вид
# одно и то же, для разбора — разные символы, и такие точки выпадали
# из контура целиком. У ULP30 пропадала замыкающая вершина, у других
# зон — рабочие. Тот же смешанный алфавит, что в единицах высот
# и в русских строках про «с» и «в».
POINT = re.compile(
    r"(\d{2})(\d{2})(\d{2})([NSНС])\s+(\d{3})(\d{2})(\d{2})([EЕWВ])"
)

# «A circle radius of 5 KM centred at 672800N 0322900E»
#
# Берём английскую строку, а не русскую. В русской буквы «с» и «в» набраны
# то кириллицей, то латиницей, то заглавными — «692700c», «0202122В», —
# и сто зон из-за этого просто пропадали, в том числе пять по Северо-Западу.
# Английская строка в АИП единообразна: латиница и заглавные N/E.
# Радиус бывает и в метрах, и полушарие бывает не северо-восточным.
#
# «A circle radius of 650 М centred at …» — четыре зоны Приволжья, причём
# у двух «М» набрана кириллицей. «centred at 641434N 1730646W» — девять
# зон Чукотки за сто восьмидесятым меридианом. Ни те, ни другие прежняя
# запись не брала, и все тринадцать уходили в список «не нанесено» —
# то есть на карте их не было вовсе.
CIRCLE_EN = re.compile(
    r"circle\s+radius\s+of\s+([\d.,]+)\s*(KM|КМ|M|М)\s+centred\s+at\s+"
    r"(\d{6})([NSНС])\s+(\d{7})([EЕWВ])",
    re.IGNORECASE,
)

# Русская строка — запасная: буквы «с» и «в» после координат набраны
# то кириллицей, то латиницей, то заглавными, поэтому берём их классом.
CIRCLE_RU = re.compile(
    r"[Оо]кружност\w*\s+радиусом\s+([\d.,]+)\s*км\s+с\s+центром\s+"
    r"(\d{6})\s*[свСВcCbBвB]\s+(\d{7})\s*[свСВcCbBвB]",
    re.IGNORECASE,
)

# Высоты: «6000/19700 AGL» сверху и «GND» снизу — в тексте двумя строками.
LIMIT = re.compile(
    r"(FL\s?\d+|UNL|\d[\d\s/]*(?:М|M|Ф|FT|КМ|KM)?\s*(?:AGL|AMSL|AMSL|ALT|MSL|ASL|GND)?)"
)

KIND = {"P": "P", "R": "R", "D": "Q"}


# Когда раздел последний раз перезаливали на сервер. Пишется в шапку файла
# зон, чтобы приложение могло дёшево спросить сервер, не вышло ли новее:
# HEAD-запрос отдаёт этот заголовок, не качая семь мегабайт.
#
# Это не то же самое, что дата действия внутри документа («Dated 12 JUN 25»):
# АИРАК меняется раз в 28 дней, а файл на сервере могут перезалить и между.
SOURCE_STAMP = "* Источник обновлён: "

USER_AGENT = "IvanSkyjournal/2.13 (+https://t.me/IvanSkyJournal)"


def fetch(path: str, target: Path) -> tuple[Path, str | None]:
    """Качает раздел АИП — до семи мегабайт, минута на медленной сети.

    Возвращает файл и дату его последнего изменения на сервере.
    """
    url = f"{AIP_BASE}/{path}"
    print(f"качаю {url}", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        target.write_bytes(response.read())
        stamp = response.headers.get("Last-Modified")
    print(f"  {target.stat().st_size // 1024} КБ", file=sys.stderr)
    return target, stamp


def text_of(pdf: Path) -> str:
    """Текстовый слой PDF. `-layout` держит колонки, иначе высоты перемешаются."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", "replace")


def aip_date(text: str) -> str | None:
    """Дата действия из колонтитула: «12 JUN 25»."""
    found = re.search(r"\b(\d{2}\s+[A-Z]{3}\s+\d{2})\b", text)
    return found.group(1) if found else None


def to_degrees(latitude: str, longitude: str) -> tuple[float, float]:
    lat = int(latitude[0:2]) + int(latitude[2:4]) / 60 + int(latitude[4:6]) / 3600
    lon = int(longitude[0:3]) + int(longitude[3:5]) / 60 + int(longitude[5:7]) / 3600
    return lat, lon


def openair_point(lat: float, lon: float, ns: str = "N", ew: str = "E") -> str:
    """Градусы-минуты-секунды без «шестидесятой секунды».

    Округление до целых секунд легко даёт 60: 58.0333° — это 58°01'60",
    и такую строку разбор координат уже не понимает. Считаем в секундах
    целиком и раскладываем обратно.
    """

    def part(value: float, degrees_width: int) -> str:
        total = round(value * 3600)
        degrees, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{degrees:0{degrees_width}d}:{minutes:02d}:{seconds:02d}"

    return f"DP {part(lat, 2)} {ns} {part(lon, 3)} {ew}"


# Дуга в контуре: «then clockwise by arc of a circle radius of 5 KM centred at
# 592100N 0281100E to 591828N 0280906E».
#
# Берётся английская запись: в русской буквы «с» и «в» после координат
# набраны то кириллицей, то латиницей. Центр дуги — **не точка контура**:
# пока фраза не разбиралась, он попадал в список точек наравне с прочими,
# и полигон заворачивал внутрь круга.
ARC = re.compile(
    r"(?:then\s+)?(anti-?clockwise|counter-?clockwise|clockwise)\s+by\s+arc\s+of\s+"
    r"a\s+circle\s+radius\s+of\s+([\d.,]+)\s*KM\s+centred\s+at\s+"
    r"(\d{6})([NS])\s+(\d{7})([EW])\s+to",
    re.IGNORECASE,
)


# «then along the state border to 592336N 0281212E» — сторона зоны идёт
# по государственной границе. Точек этой линии в АИП нет: считается,
# что она известна. Соединять её концы прямой нельзя — под Ивангородом
# граница петляет по Нарове, и срез уходит на километр.
#
# Берём английскую строку по той же причине, что и у дуг: в русской
# «с» и «в» после координат набраны то кириллицей, то латиницей.
BORDER = re.compile(
    r"(?:then\s+)?along\s+the\s+state\s+border\s+to",
    re.IGNORECASE,
)

# Русское написание той же фразы — не для разбора, а для счёта.
#
# Знак «~» у зоны должен стоять, когда сторона по границе осталась прямым
# срезом. Считать это по одной английской строке нельзя: измени редакция
# «state border» на «State boundary» — регэксп замолчит, вставка не сработает,
# стороны останутся срезами, а пометка исчезнет. Молчаливый отказ выглядел бы
# как успех, и это опаснее самого среза.
#
# Поэтому число сторон по границе считаем по обоим написаниям и берём
# большее, а помечаем зону, если легло меньше, чем упомянуто.
BORDER_RU = re.compile(
    r"по\s+(?:гос|государственной\s+)границе\s+до",
    re.IGNORECASE,
)



# Линия границы подгружается один раз: разложить её стоит секунду,
# а зон с такой стороной двадцать шесть.
_BORDER_LINE: object | None = None
_BORDER_TRIED = False


def border_line():
    """Линия государственной границы или None, если её кэша нет.

    Без неё разбор работает как раньше: стороны по границе остаются
    прямыми, зоны помечаются знаком «~». Это хуже, но честно.
    """
    global _BORDER_LINE, _BORDER_TRIED
    if not _BORDER_TRIED:
        _BORDER_TRIED = True
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from border_line import Border

            _BORDER_LINE = Border.load()
            if _BORDER_LINE is None:
                print(
                    "линии границы нет: стороны по госгранице останутся прямыми "
                    "(как её взять — в шапке tools/border_line.py)",
                    file=sys.stderr,
                )
        except Exception as error:  # noqa: BLE001 — любой отказ значит «работаем без неё»
            print(f"линия границы не подгрузилась: {error}", file=sys.stderr)
    return _BORDER_LINE


# Кириллические двойники букв полушария. Читаем оба алфавита, дальше
# по коду ходит только латиница: сравнения вроде `ns == "N"` иначе
# молча перестают срабатывать, а это не ошибка разбора, а ошибка,
# которая выглядит как исправная работа.
HEMISPHERE = {"Н": "N", "С": "S", "Е": "E", "В": "W"}


def hemisphere(letter: str) -> str:
    """Буква полушария латиницей, каким бы алфавитом её ни набрали."""
    return HEMISPHERE.get(letter, letter).upper()


def signed(lat: float, lon: float, ns: str, ew: str) -> tuple[float, float]:
    """Координаты со знаком — как их держит OpenStreetMap."""
    return (lat if ns == "N" else -lat, lon if ew == "E" else -lon)


def contour(chunk: str) -> tuple[list[str], bool]:
    """Команды OpenAir для контура зоны — в том порядке, в каком идёт АИП.

    Возвращает строки и признак «остались упрощения»: линии государственной
    границы дугами не описать, их по-прежнему срезает прямая.
    """
    lines: list[str] = []
    last: tuple[float, float, str, str] | None = None
    # Сторон по государственной границе может быть несколько, и каждая
    # берётся отдельно: одна легла на линию OSM, другая могла не лечь.
    # Считаем по обоим написаниям — какое-то из них может измениться.
    mentions = max(len(BORDER.findall(chunk)), len(BORDER_RU.findall(chunk)))
    laid = 0

    # Идём по тексту одним проходом: что встретилось раньше, то и раньше
    # в контуре. Иначе дуга не знает, от какой точки её вести.
    pattern = re.compile(
        f"(?P<arc>{ARC.pattern})|(?P<border>{BORDER.pattern})|(?P<point>{POINT.pattern})",
        re.IGNORECASE,
    )
    pending_arc: tuple[str, float, float, float] | None = None
    pending_border = False

    for found in pattern.finditer(chunk):
        if found.group("border"):
            # Следующая точка — конец стороны, идущей по границе.
            pending_border = True
            continue

        if found.group("arc"):
            arc = ARC.match(found.group("arc"))
            if not arc:
                continue
            turn = "-" if arc.group(1).lower().startswith(("anti", "counter")) else "+"
            radius_km = float(arc.group(2).replace(",", "."))
            lat, lon = to_degrees(arc.group(3), arc.group(5))
            # Полушария несём отдельно, а не знаком числа.
            #
            # Отрицательная долгота у центра дуги давала строку
            # «V X=64:45:23 N -173:56:24 E»: минус ломал и градусы (деление
            # отрицательного округляется вниз), и полушарие — а разбор
            # приложения минус не читает вовсе. Зона под бухтой Провидения
            # уезжала на полторы тысячи километров, в другое полушарие,
            # и рисовалась дугой радиусом девяносто километров вместо
            # четырёх. Настоящей зоны при этом на карте не было.
            pending_arc = (
                turn, radius_km, lat, lon,
                hemisphere(arc.group(4)), hemisphere(arc.group(6)),
            )
            continue

        point = POINT.match(found.group("point"))
        if not point:
            continue
        lat = int(point.group(1)) + int(point.group(2)) / 60 + int(point.group(3)) / 3600
        lon = int(point.group(5)) + int(point.group(6)) / 60 + int(point.group(7)) / 3600
        ns, ew = hemisphere(point.group(4)), hemisphere(point.group(8))

        if pending_arc and last is not None:
            turn, _, centre_lat, centre_lon, centre_ns, centre_ew = pending_arc
            lines.append(f"V D={turn}")
            lines.append(
                "V X=" + openair_point(centre_lat, centre_lon, centre_ns, centre_ew)[3:]
            )
            lines.append(
                "DB "
                + openair_point(last[0], last[1], last[2], last[3])[3:]
                + ", "
                + openair_point(lat, lon, ns, ew)[3:]
            )
            pending_arc = None
        elif pending_border and last is not None:
            # Сторона идёт по государственной границе. Вставляем её точки
            # между концами — но только если линия OSM их уверенно связала.
            # Отказ здесь не беда: остаётся прямая и знак «~», как было.
            #
            # В OSM координаты знаковые, в АИП — положительные с буквой
            # полушария. Переводим туда и обратно, а не отсекаем чужие
            # полушария: за сто восьмидесятым меридианом у России кончается
            # не страна, а знак долготы, и зона Чукотки с долготой W ушла бы
            # искать границу на другую сторону земного шара. Сегодня таких
            # сторон в АИП нет — но зона Провидения уже один раз уезжала
            # на полторы тысячи километров ровно из-за знака.
            line = border_line()
            piece = (
                line.between(signed(last[0], last[1], last[2], last[3]),
                             signed(lat, lon, ns, ew))
                if line is not None
                else None
            )
            if piece:
                for point_lat, point_lon in piece:
                    lines.append(
                        openair_point(
                            abs(point_lat),
                            abs(point_lon),
                            "N" if point_lat >= 0 else "S",
                            "E" if point_lon >= 0 else "W",
                        )
                    )
                laid += 1
            lines.append(openair_point(lat, lon, ns, ew))
        else:
            if last is not None and last[:2] == (lat, lon):
                continue
            lines.append(openair_point(lat, lon, ns, ew))

        pending_border = False
        last = (lat, lon, ns, ew)

    # Сторон по границе упомянуто больше, чем легло, — часть контура
    # осталась прямым срезом, и молчать об этом нельзя. Сюда же попадает
    # случай, когда разбор фразы вовсе не сработал: упомянуто, не легло
    # ни одной.
    return lines, laid < mentions


def blocks(text: str):
    """Режет текст на куски по обозначению зоны."""
    marks = [(m.start(), m.group(1)) for m in ZONE.finditer(text)]
    for index, (start, code) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        yield code, text[start:end]


# Высота в АИП записывается ровно шестью способами, и все они узнаются
# сами по себе, без оглядки на то, что стоит в строке дальше:
#
#     GND               2483 раза      нижняя граница почти всегда
#     500/1700 AMSL     1306           метры через дробь футы
#     500/1700 AGL       919           то же от земли
#     FL140              556           эшелон
#     UNL                 51           без верхнего предела
#     1500 FT AMSL         6           изредка одни футы
#
# Прежний разбор искал высоту как конец строки, разрешая после неё одно
# слово («Н24»). Строки, где дальше идёт «Бюллетень доступности» — два
# слова, — он не брал вовсе, и **742 зоны из 2627 остались без верхней
# границы**: на экране «GND–НЕИЗВЕСТНО» вместо «GND–FL140». Пилот при
# этом считает зону уходящей в небо и обходит её, хотя мог пройти под ней.
#
# Ещё семьдесят восемь зон получали верх «2» — это номер колонки таблицы
# из шапки страницы, которая повторяется в каждом куске.
# Буквы в единицах пишем классами: в PDF алфавиты смешаны.
#
# У зоны UHP305 верх записан как «1200/3900 АMSL» с кириллической «А» —
# на вид то же слово, для разбора другое. Строгий разбор брал следующую
# находку, и запретная зона над Приморьем получала верх «GND» при низе
# «GND»: то есть нулевой высоты. Та же беда уже кусала в координатах,
# где «с» и «в» набраны то кириллицей, то латиницей.
_A = "[AА]"
_M = "[MМ]"
_T = "[TТ]"
_UNITS = f"(?:{_A}GL|{_A}{_M}SL|{_A}L{_T}|{_M}SL|{_A}SL)"

HEIGHT = re.compile(
    r"FL\s?\d{2,3}"
    r"|UNL"
    r"|GND"
    rf"|\d{{1,5}}\s*/\s*\d{{1,6}}\s*(?:М|M|Ф|FT)?\s*{_UNITS}"
    rf"|\d{{1,5}}\s*(?:М|M|Ф|FT|КМ|KM)\s*{_UNITS}",
    re.IGNORECASE,
)


def tiers(chunk: str) -> int:
    """Сколько ярусов высот у зоны.

    У части зон АИП пишет не одну пару «сверху/снизу», а несколько:
    URD116 действует в трёх слоях — FL040/100-300 AMSL, FL150/FL110
    и FL630/FL350. Нанести можно только один контур, и берётся нижний
    ярус: он тот, в котором летает поршневая амфибия. Но молчать о том,
    что выше есть ещё слои, нельзя — пилот решит, что над зоной чисто.
    """
    return len(HEIGHT.findall(chunk)) // 2


# Кириллические двойники латинских букв — в файл они идти не должны.
# Разбираем в обоих алфавитах, пишем в одном: приложению отдаётся
# «AMSL», а не «АMSL», иначе оно не узнает единицу и покажет высоту
# как неразобранную — то есть ровно то, от чего чинили.
LOOKALIKE = str.maketrans({"А": "A", "М": "M", "Т": "T", "С": "C", "Е": "E"})


def limits(chunk: str) -> tuple[str, str]:
    """Верхняя и нижняя границы. Не разобрали — говорим об этом словом."""
    found = [
        re.sub(r"\s+", " ", m.group(0).strip()).translate(LOOKALIKE)
        for m in HEIGHT.finditer(chunk)
    ]
    upper = found[0] if found else "неизвестно"
    lower = found[1] if len(found) > 1 else "GND"
    return upper, lower


# «Ellipse centre coordinates 514037N 0353807Е,
#  dimensions of axes 15x10 KM, azimuth of major axis 090°»
#
# Двадцать одна зона описана эллипсом, и до сих пор все они уходили
# в список «не нанесено» — то есть на карте их не было. А описаны они
# полностью: центр, обе оси, азимут длинной. Среди них UHD412 размером
# сто пятьдесят на сто километров — опасная зона на Дальнем Востоке
# от земли до неба, которой на карте просто не существовало.
#
# Размеры — это длины осей целиком, а не полуоси: «150x100 км» значит
# сто пятьдесят километров в длину.
ELLIPSE = re.compile(
    # Между координатами и размерами вклинивается текст соседней колонки
    # («Airspace Availability Bulletin»): `pdftotext -layout` кладёт всю
    # строку подряд. Поэтому до конца строки пропускаем что угодно,
    # но не дальше — иначе склеятся соседние зоны.
    r"Ellipse\s+centre\s+coordinates\s+(\d{6})([NSНС])\s+(\d{7})([EЕWВ])\s*,?[^\n]*\n\s*"
    r"dimensions\s+of\s+axes\s+([\d.,]+)\s*[xх×]\s*([\d.,]+)\s*(KM|КМ|M|М)\s*,?[^\n]*\n?\s*"
    r"azimuth\s+of\s+major\s+axis\s+([\d.,]+)",
    re.IGNORECASE | re.DOTALL,
)

# Сколько точек в контуре эллипса. Тридцать шесть — это шаг в десять
# градусов: у зоны в полтораста километров хорда между соседними точками
# отходит от дуги на триста метров, у зоны в пятнадцать — на тридцать.
# Больше точек рисовать незачем, меньше — уже видно углами.
ELLIPSE_POINTS = 36


def ellipse_of(chunk: str):
    """Контур зоны-эллипса точками — или None, если это не эллипс."""
    found = ELLIPSE.search(chunk)
    if not found:
        return None

    lat, lon = to_degrees(found.group(1), found.group(3))
    ns, ew = hemisphere(found.group(2)), hemisphere(found.group(4))
    major = float(found.group(5).replace(",", "."))
    minor = float(found.group(6).replace(",", "."))
    if found.group(7).upper() in ("M", "М"):
        major, minor = major / 1000, minor / 1000
    azimuth = math.radians(float(found.group(8).replace(",", ".")))

    # Полуоси в километрах.
    half_major, half_minor = major / 2, minor / 2
    # Азимут считается от севера по часовой стрелке: длинная ось
    # смотрит в (sin, cos), короткая — перпендикулярно ей.
    along = (math.sin(azimuth), math.cos(azimuth))      # (восток, север)
    across = (math.cos(azimuth), -math.sin(azimuth))

    signed_lat = lat if ns == "N" else -lat
    signed_lon = lon if ew == "E" else -lon

    points = []
    for step in range(ELLIPSE_POINTS):
        angle = math.tau * step / ELLIPSE_POINTS
        east = half_major * math.cos(angle) * along[0] + half_minor * math.sin(angle) * across[0]
        north = half_major * math.cos(angle) * along[1] + half_minor * math.sin(angle) * across[1]
        point_lat = signed_lat + north / 111.32
        # Градус долготы меряем на широте самой точки, а не центра.
        # У эллипса в сто шестьдесят километров концы длинной оси уходят
        # от центра на четверть градуса, и общий масштаб врал там
        # на восемьсот метров — столько же, сколько срезала прямая
        # у приграничных зон, ради чего вся ночная возня и затевалась.
        east_km = 111.32 * math.cos(math.radians((signed_lat + point_lat) / 2))
        point_lon = signed_lon + east / east_km
        points.append(
            openair_point(
                abs(point_lat),
                abs(point_lon),
                "N" if point_lat >= 0 else "S",
                "E" if point_lon >= 0 else "W",
            )
        )
    points.append(points[0])
    return points


# «The route centre line: 421500N 1330200E – 422800N 1333000E
#  ±5 KM wide from the centre line»
#
# Коридор вдоль оси маршрута — последняя форма, которой в АИП описывают
# зону. Такая одна: UHD413 на Дальнем Востоке, опасная, от земли до FL150.
CORRIDOR = re.compile(
    r"[±\+\-]\s*([\d.,]+)\s*(KM|КМ)\s+wide\s+from\s+the\s+cent(?:re|er)\s+line",
    re.IGNORECASE,
)


def corridor_of(chunk: str):
    """Контур коридора вокруг оси маршрута — или None."""
    width = CORRIDOR.search(chunk)
    if not width:
        return None

    axis = []
    for found in POINT.finditer(chunk):
        lat = int(found.group(1)) + int(found.group(2)) / 60 + int(found.group(3)) / 3600
        lon = int(found.group(5)) + int(found.group(6)) / 60 + int(found.group(7)) / 3600
        if hemisphere(found.group(4)) == "S":
            lat = -lat
        if hemisphere(found.group(8)) == "W":
            lon = -lon
        axis.append((lat, lon))
    if len(axis) < 2:
        return None

    # «±5 KM wide» значит пять километров в каждую сторону, то есть
    # полоса шириной десять. Отступ от оси — это и есть записанное число.
    half = float(width.group(1).replace(",", "."))

    # Идём по оси вперёд, откладывая точки слева, и назад — справа.
    left, right = [], []
    for index in range(len(axis) - 1):
        (lat_a, lon_a), (lat_b, lon_b) = axis[index], axis[index + 1]
        # Масштаб долготы — по середине отрезка оси, а не по её началу.
        east_km = 111.32 * math.cos(math.radians((lat_a + lat_b) / 2))
        north = (lat_b - lat_a) * 111.32
        east = (lon_b - lon_a) * east_km
        span = math.hypot(north, east)
        if span == 0:
            continue
        # Перпендикуляр к отрезку, в километрах.
        off_north, off_east = -east / span * half, north / span * half
        for lat, lon in ((lat_a, lon_a), (lat_b, lon_b)):
            left.append((lat + off_north / 111.32, lon + off_east / east_km))
            right.append((lat - off_north / 111.32, lon - off_east / east_km))

    ring = left + right[::-1]
    points = [
        openair_point(
            abs(lat), abs(lon),
            "N" if lat >= 0 else "S",
            "E" if lon >= 0 else "W",
        )
        for lat, lon in ring
    ]
    points.append(points[0])
    return points


def circle_of(chunk: str):
    """Зона-окружность целиком — но не кусок дуги.

    В АИП дуга описана словами «by arc of a circle radius of 5 KM centred
    at …», и поиск окружности находил в них ту же «circle radius of».
    Зона ULP3, у которой контур из точек и одной дуги, выходила ровным
    кругом не на своём месте.
    """
    if ARC.search(chunk):
        return None
    found = CIRCLE_EN.search(chunk)
    if found:
        radius = float(found.group(1).replace(",", "."))
        # Метры приводим к километрам: «650 М» — это 0.65 км, а не 650.
        unit = found.group(2).upper()
        radius_km = radius / 1000 if unit in ("M", "М") else radius
        lat, lon = to_degrees(found.group(3), found.group(5))
        return radius_km, lat, lon, hemisphere(found.group(4)), hemisphere(found.group(6))

    found = CIRCLE_RU.search(chunk)
    if not found:
        return None
    radius_km = float(found.group(1).replace(",", "."))
    lat, lon = to_degrees(found.group(2), found.group(3))
    return radius_km, lat, lon, "N", "E"


# Заголовок зоны в ENR 2 — это пара строк: русская и её английский дубль.
#
#     ИРКУТСК ДИСПЕТЧЕРСКИЙ РАЙОН     РДЦ Иркутск   129.000 MHz
#     IRKUTSK CTA                     Irkutsk ACC
#
#     СЕКТОР 1                        H24           133.400 MHz
#     SECTOR 1                                      133.800 MHz
#
# По одной строке заголовок не опознать: заглавными в этом разделе набрано
# многое. А вот русская строка, под которой стоит латинская, — это всегда
# начало зоны.
RUSSIAN_TITLE = re.compile(r"^\s{0,8}([А-ЯЁ][А-ЯЁ0-9 \-/№\.]{2,58}?)(?:\s{3,}.*)?$")
ENGLISH_TITLE = re.compile(r"^\s{0,8}([A-Z][A-Z0-9 \-/\.]{2,58}?)(?:\s{3,}.*)?$")

# Частота органа: «129.000 MHz». Пилоту она нужнее номера сектора.
#
# 121.500 пропускаем: это аварийный канал, и в АИП он стоит у районов
# полётной информации первым. Пилот, увидев его рядом с именем зоны,
# запросил бы разрешение на аварийной частоте вместо рабочей.
FREQUENCY = re.compile(r"(\d{3}\.\d{3})\s*MHz")
EMERGENCY = "121.500"

# Вертикальные границы. В ENR 2 они пишутся по-разному:
#
#     FL100 - UNL
#     above FL265 - UNL Class A
#     GND - below FL100 Class G
#     above 2900 M/9500 FT AMSL - UNL
#
# Последний вид — метры с футами в скобках — сначала не понимался вовсе,
# и таким зонам подставлялась нижняя граница «GND». Это опаснее пропуска:
# район, который начинается на двух с половиной километрах, объявлялся
# идущим от земли, и приложение сказало бы пилоту на трёхстах метрах,
# что он внутри. Умолчания больше нет: не разобрали — так и написано.
LEVEL = r"(?:FL\s?\d+|UNL|GND|SFC|\d+\s*(?:M|М)(?:/\d+\s*(?:FT|ФТ))?(?:\s*(?:AMSL|AGL|MSL|ASL))?)"
BAND = re.compile(
    rf"^\s*(?:above\s+)?({LEVEL})\s*[-–]\s*(?:below\s+)?({LEVEL})",
    re.M | re.IGNORECASE,
)


def airspace_blocks(text: str):
    """Режет ENR 2 на зоны по парам заголовков «русский / английский»."""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for index in range(len(lines) - 1):
        russian = RUSSIAN_TITLE.match(lines[index])
        if not russian:
            continue
        # Английский дубль стоит следующей строкой — но не всегда:
        # колонка «Примечания» переносится и разрывает пару. Из-за строгого
        # правила терялось восемьдесят зон, и их контуры прирастали
        # к предыдущей: «СЕКТОР ВЕЛИКИЕ ЛУКИ» получал сорок точек вместо
        # двенадцати, с вершиной на Новой Земле. Смотрим три строки.
        english = None
        for shift in (1, 2, 3):
            if index + shift >= len(lines):
                break
            english = ENGLISH_TITLE.match(lines[index + shift])
            if english:
                break
        if not english:
            continue
        title = russian.group(1).strip()
        # Отсеиваем колонтитулы и служебные строки.
        if title in {"AIP", "RUSSIA"} or title.startswith("ENR"):
            continue
        marks.append((index, title))

    for position, (index, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        yield title, "\n".join(lines[index:end])


def convert_airspace(text: str) -> tuple[list[str], int, list[str]]:
    """Диспетчерские районы и секторы из ENR 2.1 и 2.2.

    Это не «куда нельзя», а «у кого спрашивать»: у каждого куска свой орган
    и своя частота. Поэтому имя зоны собирается как «название · частота» —
    в полёте нужна частота, а не номер сектора.
    """
    out: list[str] = []
    taken = 0
    skipped: list[str] = []
    area_name = ""

    for title, chunk in airspace_blocks(text):
        points = POINT.findall(chunk)
        if len(points) < 3:
            continue

        # Полос в блоке бывает несколько — по классам воздушного пространства
        # или по частям района. Одна строка OpenAir описывает один диапазон,
        # и брать надо **самый низкий**: у Московского узлового района части
        # начинаются с 450 и с 900 метров, и ошибка в большую сторону — это
        # молчание приложения ровно на рабочей высоте амфибии.
        bands = list(BAND.finditer(chunk))
        if bands:
            def floor_of(match) -> float:
                text = match.group(1).upper().replace(" ", "")
                if text in ("GND", "SFC"):
                    return 0.0
                if text.startswith("FL"):
                    return float(re.sub(r"[^\d]", "", text) or 0) * 30.48
                digits = re.match(r"(\d+)", text)
                return float(digits.group(1)) if digits else 1e9

            lowest = min(bands, key=floor_of)
            upper = lowest.group(2).strip()
            lower = lowest.group(1).strip()
        else:
            upper = "границы не разобраны"
            lower = "границы не разобраны"
        # В АИП порядок «нижняя - верхняя», а бывает и «above FL265 - UNL».
        if lower.upper().startswith("FL") and upper.upper() == "UNL":
            pass
        frequency = next(
            (f for f in FREQUENCY.finditer(chunk) if f.group(1) != EMERGENCY),
            None,
        )

        # «СЕКТОР 1» сам по себе ничего не говорит: секторов с таким именем
        # в документе два десятка, и все у разных районов. Держим последний
        # район и подставляем его — но только пронумерованным секторам.
        #
        # Именованные секторы («СЕКТОР НВП САНКТ-ПЕТЕРБУРГ 1») идут в ENR 2.2
        # сплошным списком, без повторения заголовка района, и подстановка
        # приписывала им первый попавшийся: под Петербургом на экране
        # появлялся «РОСТОВ-НА-ДОНУ». Место в их имени уже есть, чужое
        # добавлять нельзя.
        short = title.replace("ДИСПЕТЧЕРСКИЙ РАЙОН", "ДР").replace("  ", " ").strip()
        numbered = re.fullmatch(r"СЕКТОР\s+([0-9]{1,3}[А-ЯA-Z]?)", short)
        if numbered and area_name:
            short = f"{area_name}, сектор {numbered.group(1).lower()}"
        elif not short.startswith("СЕКТОР"):
            area_name = short.replace(" ДР", "").replace(" РПИ", "").strip()

        name = short if not frequency else f"{short} · {frequency.group(1)}"

        # Тот же знак «~», что и у зон ENR 5: контур упрощён, потому что
        # часть границы описана словами.
        if "госгранице" in chunk or "государственной границе" in chunk:
            name = f"~{name}"
        lines = ["AC CTR", f"AN {name}", f"AH {upper}", f"AL {lower}"]
        seen = set()
        for point in points:
            lat = int(point[0]) + int(point[1]) / 60 + int(point[2]) / 3600
            lon = int(point[4]) + int(point[5]) / 60 + int(point[6]) / 3600
            key = (round(lat, 6), round(lon, 6), point[3], point[7])
            if key in seen:
                continue
            seen.add(key)
            lines.append(openair_point(lat, lon, point[3], point[7]))

        if len(seen) < 3:
            skipped.append(title)
            continue

        out.extend(lines)
        out.append("")
        taken += 1

    return out, taken, skipped


def convert(text: str, fir: str | None) -> tuple[list[str], int, list[str], list[str]]:
    out: list[str] = []
    taken = 0
    skipped: list[str] = []
    simplified: list[str] = []
    layered: list[str] = []

    for code, chunk in blocks(text):
        if fir and not code.startswith(fir):
            continue

        kind = KIND.get(code[2], "R")
        upper, lower = limits(chunk)

        # Контур бывает описан не только точками.
        #
        # «далее по государственной границе до …» и «по дуге радиусом 5 км
        # с центром …» — это линии, которых в списке точек нет: OpenAir
        # соединит соседние точки прямой, и граница зоны срежет угол.
        # У приграничных зон срез уходит на десятки километров.
        #
        # Такую зону нельзя ни выбросить (она есть), ни показать молча
        # (её край не там). Помечаем именем: «~» перед кодом значит
        # «контур упрощён, смотрите АИП».
        header = [f"AC {kind}", f"AH {upper}", f"AL {lower}"]

        ellipse = ellipse_of(chunk) or corridor_of(chunk)
        if ellipse:
            body = ellipse
            rough = False
            name = code
            lines = [header[0], f"AN {name}", header[1], header[2]] + body
            out.extend(lines)
            out.append("")
            taken += 1
            continue

        circle = circle_of(chunk)
        if circle:
            radius_km, lat, lon, circle_ns, circle_ew = circle
            body = [
                "V X=" + openair_point(lat, lon, circle_ns, circle_ew)[3:],
                # OpenAir меряет радиус в морских милях, АИП — в километрах.
                f"DC {radius_km / 1.852:.3f}",
            ]
            rough = False
        else:
            body, rough = contour(chunk)
            if sum(1 for line in body if line.startswith(("DP", "DB"))) < 3:
                skipped.append(code)
                continue

        # «~» перед кодом значит «край приблизителен, смотрите АИП».
        # Остаётся только у зон, чья граница идёт по государственной:
        # дуги теперь строятся как дуги.
        #
        # «=» значит «у зоны несколько ярусов высот, нанесён нижний».
        # Знак другой, потому что и беда другая: там врёт край, здесь —
        # потолок. Оба видны прямо на карте, а не только в шапке файла:
        # шапку в полёте не читают.
        name = f"~{code}" if rough else code
        if rough:
            simplified.append(code)
        if tiers(chunk) > 1:
            name = f"={name}"
            layered.append(code)

        lines = [header[0], f"AN {name}", header[1], header[2]] + body

        out.extend(lines)
        out.append("")
        taken += 1

    if layered:
        print(
            f"  несколько ярусов высот у {len(layered)} зон, нанесён нижний: "
            + " ".join(layered),
            file=sys.stderr,
        )
    if simplified:
        print(
            f"  контур упрощён у {len(simplified)} зон: "
            + " ".join(simplified[:12])
            + (" …" if len(simplified) > 12 else ""),
            file=sys.stderr,
        )
    return out, taken, skipped, simplified, layered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, help="взять готовый PDF вместо скачивания")
    parser.add_argument("--out", type=Path, default=Path("russia-aip.txt"))
    parser.add_argument("--fir", help="оставить один район, например UL")
    parser.add_argument(
        "--section",
        choices=sorted(SECTIONS),
        default="enr5",
        help="enr5 — запретные зоны и зоны ограничений (по умолчанию); "
             "enr2 — диспетчерские районы и секторы с частотами",
    )
    options = parser.parse_args()

    sources: list[Path] = []
    stamps: list[str] = []
    if options.pdf:
        sources = [options.pdf]
    else:
        folder = Path(tempfile.gettempdir())
        for path in SECTIONS[options.section]:
            target = folder / Path(path).name
            downloaded, stamp = fetch(path, target)
            sources.append(downloaded)
            if stamp:
                stamps.append(stamp)

    zones: list[str] = []
    taken = 0
    skipped: list[str] = []
    simplified: list[str] = []
    layered: list[str] = []
    date: str | None = None

    for pdf in sources:
        text = text_of(pdf)
        date = date or aip_date(text)
        if options.section == "enr2":
            part, count, lost = convert_airspace(text)
        else:
            part, count, lost, rough, tiered = convert(text, options.fir)
            simplified += rough
            layered += tiered
        zones += part
        taken += count
        skipped += lost

    title = {
        "enr5": "Запретные зоны, зоны ограничения полётов и опасные (ENR 5.1)",
        "enr2": "Диспетчерские районы и секторы с частотами (ENR 2.1, 2.2)",
    }[options.section]

    header = [
        "*" * 78,
        f"* Зоны из АИП России: {title}",
        f"* Dated {date or 'не определена'}",
        f"* Дата АИП: {date or 'не определена'}",
        "* Источник: ЦАИ ГА, caica.ru",
    ]
    if stamps:
        # Строку читает приложение: по ней оно спрашивает сервер,
        # не появилось ли на месте того же файла что-то новее.
        header.append(SOURCE_STAMP + stamps[0])
    header += [
        f"* Собрано {taken} зон",
        "*",
        "* Это ПЕРЕСКАЗ документа, а не сам документ: файл собран разбором PDF.",
        "* Сверяйтесь с АИП. Не для навигации.",
    ]

    if simplified:
        header += [
            "*",
            f"* КОНТУР УПРОЩЁН у {len(simplified)} зон: в АИП их граница описана",
            "* линией государственной границы, а точек этой линии в документе нет.",
            "* Такие зоны помечены знаком «~» перед кодом — их край соединён",
            "* прямой и на карте приблизителен: у границы расхождение бывает",
            "* в десятки километров.",
            "*",
            "* У остальных приграничных зон линия границы взята",
            "* из OpenStreetMap — это НЕ АИП. Совпадение проверено по тому,",
            "* что линия разделяет страны, но источник сторонний.",
        ]

    if layered:
        header += [
            "*",
            f"* НЕСКОЛЬКО ЯРУСОВ ВЫСОТ у {len(layered)} зон: в АИП они действуют",
            "* в двух-трёх слоях по высоте, а нанести можно один. Нанесён нижний —",
            "* тот, в котором летает поршневая машина. Помечены знаком «=» перед",
            "* кодом; если идёте над такой зоной, смотрите АИП, а не карту:",
            "*   " + " ".join(layered),
        ]

    if skipped:
        header += [
            "*",
            f"* НЕ НАНЕСЕНО {len(skipped)} зон: их границы описаны в АИП словами",
            "* (эллипс, коридор вдоль оси маршрута, государственная граница).",
            "* Смотрите их в самом АИП:",
        ]
        line = "*   "
        for code in skipped:
            if len(line) + len(code) > 74:
                header.append(line)
                line = "*   "
            line += code + " "
        header.append(line.rstrip())

    header += ["*" * 78, ""]

    options.out.write_text("\n".join(header + zones), encoding="utf-8")
    print(
        f"{options.out}: зон {taken}, не нанесено {len(skipped)}, дата АИП {date}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

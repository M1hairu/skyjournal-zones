#!/usr/bin/env python3
"""Государственная граница России как линия — чтобы контуры зон не срезали углы.

У части зон АИП пишет границу словами: «далее по государственной границе
до 592336N 0281212E». Точек этой линии в документе нет, и разбор соединяет
соседние вершины прямой. У зоны ULP3 под Ивангородом такой срез уходит
на километр: граница идёт по извивам Наровы, прямая режет их напрямик.
Километр — это разница между «иду вдоль зоны» и «я в ней».

Линия берётся из OpenStreetMap, из отношения границы России:

    curl -d 'data=[out:json][timeout:800];rel(60189);way(r);out geom;' \
         https://overpass-api.de/api/interpreter -o /tmp/border-full.json

Запрос тяжёлый — 12 МБ и несколько минут. Зато отдаёт то, чего не отдаёт
выборка по рамке: под тегом `admin_level=2` сухопутной границы с Финляндией
в OSM попросту нет, линии там помечены иначе, а роль границы задаётся
отношением. Поэтому берём отношение целиком.

Дальше 5050 отрезков склеиваются по общим концам в 32 цепочки, из которых
первая — вся граница страны одной линией в 156 тысяч точек.

    python3 tools/border_line.py --check          # проверка на зоне ULP3
    python3 tools/border_line.py --build          # разложить цепочки в кэш

Как модуль:

    from border_line import Border
    border = Border.load()
    points = border.between((59.307, 28.151), (59.393, 28.203))

`between` возвращает точки границы между двумя вершинами контура или None,
если вставлять нельзя — об этом ниже, в самой функции.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

RAW = Path("/tmp/border-full.json")
CACHE = Path("/tmp/border-chains.json")

# Насколько далеко от линии границы может лежать точка АИП, чтобы считать,
# что она эту границу и обозначает. Координаты в АИП записаны с точностью
# до секунды — это тридцать метров по широте; ещё столько же добавляет то,
# что по реке граница идёт фарватером, а не берегом. Полкилометра — запас
# на это, но не настолько большой, чтобы принять за границу точку в поле.
NEAR_KM = 0.6

# Насколько точку линии можно сдвинуть при прореживании. OSM размечает
# речную границу подробно — под Калининградом выходит семнадцать метров
# на точку, три с половиной тысячи точек на одну сторону зоны. В кабине
# такая подробность не видна никогда: на самом крупном масштабе карты
# это доли пикселя, а рисуется она каждый кадр. Двадцать пять метров —
# граница, за которой прореживание начало бы съедать настоящие изгибы.
THIN_M = 25.0

# Во сколько раз участок границы может быть длиннее прямой между концами.
# Граница петляет, поэтому полуторакратная длина обычна. Но если путь
# оказался вдесятеро длиннее, значит выбран не тот из двух возможных —
# по замкнутой линии между точками всегда два пути, и уйти можно вокруг
# всей страны. Такое лучше оставить прямой.
MAX_DETOUR = 4.0


def apart(lon_a: float, lon_b: float) -> float:
    """Разница долгот с оглядкой на сто восьмидесятый меридиан.

    179.99 и −179.99 — это девятьсот метров, а не полкруга. У России
    граница через этот меридиан идёт (Чукотка), и без такой поправки
    два соседних пограничных знака оказывались в шестнадцати тысячах
    километров друг от друга — со всеми вытекающими: прореживание
    съедало настоящие вершины, а проверка обхода ничего не замечала,
    потому что раздувались обе величины сразу.
    """
    gap = lon_a - lon_b
    if gap > 180:
        gap -= 360
    elif gap < -180:
        gap += 360
    return gap


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние в километрах. Плоско — участки здесь короткие."""
    middle = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111.32
    dx = apart(a[1], b[1]) * 111.32 * math.cos(middle)
    return math.hypot(dx, dy)


def thin(points: list[tuple[float, float]], limit_m: float = THIN_M):
    """Прореживание Дугласа — Пекера: выбрасывает точки, которые лежат
    ближе `limit_m` к отрезку между соседями. Форму линии сохраняет —
    именно её алгоритм и бережёт, в отличие от «брать каждую пятую»."""
    if len(points) < 3:
        return points
    # Отрицательный допуск загонял разбиение в вечный цикл: расстояние
    # никогда не бывает меньше него, точка-виновник не находится, и в стек
    # ложится та же пара. Из `between` такое не приходит, но защита стоит
    # там, где ошибка возможна, а не там, где её сегодня нет.
    limit_m = max(0.0, limit_m)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        # Долготу считаем не как есть, а как смещение от начала участка:
        # так поправка на сто восьмидесятый меридиан входит сама собой,
        # и точки по разные стороны от него не разъезжаются на полкруга.
        origin = points[start][1]
        # Долготу сжимаем по широте, иначе на севере «метр» вдоль
        # параллели вдвое короче метра вдоль меридиана.
        scale = math.cos(math.radians(points[start][0]))
        ax, ay = 0.0, points[start][0]
        bx, by = apart(points[end][1], origin) * scale, points[end][0]
        span = math.hypot(bx - ax, by - ay)

        worst, worst_at = 0.0, start
        for index in range(start + 1, end):
            px, py = apart(points[index][1], origin) * scale, points[index][0]
            if span < 1e-12:
                gap = math.hypot(px - ax, py - ay)
            else:
                gap = abs((by - ay) * px - (bx - ax) * py + bx * ay - by * ax) / span
            if gap > worst:
                worst, worst_at = gap, index

        if worst * 111_320 > limit_m:
            keep[worst_at] = True
            stack.append((start, worst_at))
            stack.append((worst_at, end))

    return [point for point, taken in zip(points, keep) if taken]


def cell_of(point: tuple[float, float]) -> tuple[int, int]:
    """Клетка сетки по градусу.

    Округление вниз, а не усечение: `int(-179.5)` даёт −179, и точки
    южного да западного полушарий сваливаются в клетку соседа. Само
    по себе это не дырка (клетки только сливаются), но заворот через
    сто восьмидесятый меридиан считать становится нечем.
    """
    return (math.floor(point[0]), math.floor(point[1]))


class Border:
    """Линия государственной границы, разложенная по цепочкам."""

    def __init__(self, chains: list[list[tuple[float, float]]]):
        self.chains = chains
        # Сетка по градусу: перебирать 156 тысяч точек на каждый запрос
        # можно, но зон двадцать шесть, а точек в контурах сотни.
        self.grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for ci, chain in enumerate(chains):
            for pi, point in enumerate(chain):
                self.grid.setdefault(cell_of(point), []).append((ci, pi))

    @classmethod
    def load(cls, path: Path = CACHE) -> "Border | None":
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        return cls([[(p[0], p[1]) for p in chain] for chain in raw])

    def nearest(self, point: tuple[float, float]) -> tuple[float, int, int] | None:
        """Ближайшая точка границы: расстояние, цепочка, номер в цепочке."""
        best: tuple[float, int, int] | None = None
        base = cell_of(point)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                # Клетки 179 и −180 лежат рядом на земле, хотя по числу
                # далеки. Заворачиваем, иначе граница «пропадает» ровно
                # на сто восьмидесятом меридиане — то есть на Чукотке.
                cell_lon = ((base[1] + dx + 180) % 360) - 180
                for ci, pi in self.grid.get((base[0] + dy, cell_lon), ()):
                    distance = km(point, self.chains[ci][pi])
                    if best is None or distance < best[0]:
                        best = (distance, ci, pi)
        return best

    def between(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> list[tuple[float, float]] | None:
        """Точки границы между двумя вершинами контура — или None.

        None значит «вставлять нельзя», и это не сбой, а честный ответ.
        Так бывает в трёх случаях, и каждый из них лучше оставить прямой,
        чем угадать: одна из точек далеко от границы (значит, эта сторона
        зоны идёт не по ней); точки попали на разные цепочки (между ними
        разрыв в данных OSM, и что там — неизвестно); путь получился
        вчетверо длиннее прямой (выбрана не та сторона замкнутой линии).
        """
        first = self.nearest(start)
        second = self.nearest(end)
        if not first or not second:
            return None
        if first[0] > NEAR_KM or second[0] > NEAR_KM:
            return None
        if first[1] != second[1]:
            return None

        chain = self.chains[first[1]]
        low, high = sorted((first[2], second[2]))
        piece = chain[low : high + 1]
        if len(piece) < 3:
            return None
        # Порядок обхода контура задаёт АИП, а не порядок точек в OSM.
        if first[2] > second[2]:
            piece = piece[::-1]

        length = sum(km(piece[i], piece[i + 1]) for i in range(len(piece) - 1))
        straight = km(start, end)
        if straight > 0 and length > straight * MAX_DETOUR:
            return None
        # Прореживаем не жёстким шагом, а по размеру самой стороны:
        # двадцать пять метров на десятикилометровой стороне не видно,
        # а на стороне длиной в километр это уже заметный сдвиг края.
        # Полпроцента длины держит расхождение площади ниже процента
        # у зоны любого размера.
        limit = min(THIN_M, length * 1000 * 0.005)

        # Концы контура АИП уже стоят своими DP — отдаём только середину.
        #
        # Порядок здесь важен, и я на нём ошибся: `thin(piece)[1:-1]`
        # выглядит тем же самым, но прореживание бережёт крайние точки,
        # а срез после него выбрасывает их вместе с ближайшими соседями.
        # У зоны URR553 контур из-за этого прыгал от точки АИП сразу
        # на пятнадцать километров вдоль границы. Сначала снимаем концы
        # (они дублируют точки самого документа), потом прореживаем то,
        # что осталось.
        return thin(piece[1:-1], limit)


def build() -> Border:
    """Склеивает отрезки OSM в цепочки и кладёт в кэш."""
    if not RAW.exists():
        raise SystemExit(f"нет {RAW} — как её взять, написано в шапке файла")

    data = json.loads(RAW.read_text())
    ways: list[list[tuple[float, float]]] = []
    for element in data.get("elements", []):
        geometry = element.get("geometry") or []
        if len(geometry) >= 2:
            ways.append([(round(p["lat"], 7), round(p["lon"], 7)) for p in geometry])

    ends: dict[tuple[float, float], list[int]] = {}
    for index, way in enumerate(ways):
        ends.setdefault(way[0], []).append(index)
        ends.setdefault(way[-1], []).append(index)

    used = [False] * len(ways)
    chains: list[list[tuple[float, float]]] = []
    for index in range(len(ways)):
        if used[index]:
            continue
        used[index] = True
        chain = list(ways[index])
        for forward in (False, True):
            while True:
                tip = chain[-1] if forward else chain[0]
                following = next((j for j in ends.get(tip, ()) if not used[j]), None)
                if following is None:
                    break
                used[following] = True
                way = ways[following]
                piece = way if way[0] == tip else way[::-1]
                if forward:
                    chain.extend(piece[1:])
                else:
                    chain[:0] = piece[::-1][:-1]
        chains.append(chain)

    chains.sort(key=len, reverse=True)
    CACHE.write_text(json.dumps([[list(p) for p in c] for c in chains]))
    print(
        f"{CACHE}: цепочек {len(chains)}, точек {sum(len(c) for c in chains)}, "
        f"самая длинная {len(chains[0])}",
        file=sys.stderr,
    )
    return Border(chains)


def check(border: Border) -> None:
    """Проверка на зоне ULP3 — той самой, что срезает Нарову."""
    start = (59 + 18 / 60 + 28 / 3600, 28 + 9 / 60 + 6 / 3600)
    end = (59 + 23 / 60 + 36 / 3600, 28 + 12 / 60 + 12 / 3600)
    piece = border.between(start, end)
    if piece is None:
        print("ULP3: участок не взялся", file=sys.stderr)
        return
    full = [start, *piece, end]
    length = sum(km(full[i], full[i + 1]) for i in range(len(full) - 1))
    print(
        f"ULP3: {len(piece)} точек границы, длина {length:.2f} км "
        f"против прямой {km(start, end):.2f} км",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="разложить цепочки в кэш")
    parser.add_argument("--check", action="store_true", help="проверить на ULP3")
    options = parser.parse_args()

    border = build() if options.build else Border.load()
    if border is None:
        raise SystemExit(f"нет {CACHE} — сначала --build")
    if options.check:
        check(border)


if __name__ == "__main__":
    main()

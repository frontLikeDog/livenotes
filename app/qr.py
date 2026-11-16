# -*- coding: utf-8 -*-
"""最小二维码编码器（字节模式，纠错等级 M，版本 1–10）。

只服务分享卡片上的「手机扫一下」：内容是一条局域网链接，长度不超过
几百字节。没有第三方依赖，输出布尔矩阵，前端用 Canvas 画出来。

实现按 ISO/IEC 18004：字节模式、GF(256) Reed-Solomon、8 种掩码按罚分
选优。版本 10 以上不支持——链接再长就应该改用可点击的分享方式。
"""
from __future__ import annotations

#: version -> (总码字, 每块纠错码字, 组1块数, 组1数据码字, 组2块数, 组2数据码字)
_BLOCKS_M: dict[int, tuple[int, int, int, int, int, int]] = {
    1: (26, 10, 1, 16, 0, 0),
    2: (44, 16, 1, 28, 0, 0),
    3: (70, 26, 1, 44, 0, 0),
    4: (100, 18, 2, 32, 0, 0),
    5: (134, 24, 2, 43, 0, 0),
    6: (172, 16, 4, 27, 0, 0),
    7: (196, 18, 4, 31, 0, 0),
    8: (242, 22, 2, 38, 2, 39),
    9: (292, 22, 3, 36, 2, 37),
    10: (346, 26, 4, 43, 1, 44),
}

_ALIGN = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30),
    6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46),
    10: (6, 28, 50),
}

#: GF(256) 指数 / 对数表，本原多项式 0x11D
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gen_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _mul(coef, _EXP[i])
        poly = nxt
    return poly


def _rs(data: list[int], ec: int) -> list[int]:
    gen = _gen_poly(ec)
    msg = list(data) + [0] * ec
    for i in range(len(data)):
        factor = msg[i]
        if not factor:
            continue
        for j, coef in enumerate(gen):
            msg[i + j] ^= _mul(coef, factor)
    return msg[-ec:]


def _bits(value: int, length: int) -> list[int]:
    return [(value >> i) & 1 for i in range(length - 1, -1, -1)]


def capacity_bytes(version: int) -> int:
    total, ec, n1, d1, n2, d2 = _BLOCKS_M[version]
    data = n1 * d1 + n2 * d2
    # 4 位模式 + 8/16 位长度，向下取整到字节
    count_bits = 8 if version <= 9 else 16
    return (data * 8 - 4 - count_bits) // 8


def _pick_version(length: int) -> int:
    for version in range(1, 11):
        if length <= capacity_bytes(version):
            return version
    raise ValueError("内容太长，二维码放不下（最多 %d 字节）" % capacity_bytes(10))


def _codewords(data: bytes, version: int) -> list[int]:
    total, ec, n1, d1, n2, d2 = _BLOCKS_M[version]
    count_bits = 8 if version <= 9 else 16
    bits = _bits(0b0100, 4) + _bits(len(data), count_bits)
    for byte in data:
        bits.extend(_bits(byte, 8))
    data_cw = n1 * d1 + n2 * d2
    if len(bits) > data_cw * 8:
        raise ValueError("内容太长")
    bits.extend([0] * min(4, data_cw * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(str(b) for b in bits[i:i + 8]), 2)
             for i in range(0, len(bits), 8)]
    pads = (0xEC, 0x11)
    while len(words) < data_cw:
        words.append(pads[(len(words) - len(bits) // 8) % 2])
    blocks: list[list[int]] = []
    offset = 0
    for _ in range(n1):
        blocks.append(words[offset:offset + d1])
        offset += d1
    for _ in range(n2):
        blocks.append(words[offset:offset + d2])
        offset += d2
    ec_blocks = [_rs(block, ec) for block in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec):
        for block in ec_blocks:
            out.append(block[i])
    return out


def _bch(value: int, poly: int, bits: int, mask: int = 0) -> int:
    rem = value << bits
    while rem.bit_length() - 1 >= bits:
        rem ^= poly << (rem.bit_length() - 1 - bits)
    return ((value << bits) | rem) ^ mask


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.reserved = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = bool(dark)
        self.reserved[row][col] = True

    def _finder(self, row: int, col: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if not (0 <= r < self.size and 0 <= c < self.size):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                dark = inside and (dr in (0, 6) or dc in (0, 6)
                                   or (2 <= dr <= 4 and 2 <= dc <= 4))
                self.set(r, c, dark)

    def _alignment(self, row: int, col: int) -> None:
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                self.set(row + dr, col + dc, max(abs(dr), abs(dc)) != 1)

    def _timing(self) -> None:
        for i in range(8, self.size - 8):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)

    def _reserve_format(self) -> None:
        for i in range(9):
            self.set(8, i, False)
            self.set(i, 8, False)
        for i in range(8):
            self.set(8, self.size - 1 - i, False)
        for i in range(7):
            self.set(self.size - 1 - i, 8, False)
        # 固定黑点（dark module）
        self.set(4 * self.version + 9, 8, True)

    def _draw_format(self, mask: int) -> None:
        ec_bits = 0b00  # 纠错等级 M
        value = (ec_bits << 3) | mask
        fmt = _bch(value, 0x537, 10, 0x5412)
        # 第一份：左上角竖排 + 左下角；第二份：右上角 + 左上角横排。
        # 坐标以 (行, 列) 表示，和 ISO 18004 的放置顺序一致。
        for i in range(15):
            bit = (fmt >> i) & 1 == 1
            if i < 6:
                self.dark[i][8] = bit
            elif i < 8:
                self.dark[i + 1][8] = bit
            else:
                self.dark[self.size - 15 + i][8] = bit
            if i < 8:
                self.dark[8][self.size - 1 - i] = bit
            elif i == 8:
                self.dark[8][7] = bit
            else:
                self.dark[8][14 - i] = bit

    def _reserve_version(self) -> None:
        if self.version < 7:
            return
        for i in range(18):
            row, col = i // 3, self.size - 11 + i % 3
            self.set(row, col, False)
            self.set(col, row, False)

    def _draw_version(self) -> None:
        if self.version < 7:
            return
        info = _bch(self.version, 0x1F25, 12)
        for i in range(18):
            bit = (info >> i) & 1 == 1
            row, col = i // 3, self.size - 11 + i % 3
            self.dark[row][col] = bit
            self.dark[col][row] = bit

    def _place_data(self, codewords: list[int], mask: int) -> None:
        bits: list[int] = []
        for word in codewords:
            bits.extend(_bits(word, 8))
        n = 0
        upward = True
        col = self.size - 1
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(self.size - 1, -1, -1) if upward else range(self.size)
            for row in rows:
                for c in (col, col - 1):
                    if self.reserved[row][c]:
                        continue
                    bit = bits[n] if n < len(bits) else 0
                    n += 1
                    if _mask(mask, row, c):
                        bit ^= 1
                    self.dark[row][c] = bool(bit)
            upward = not upward
            col -= 2

    def draw(self) -> None:
        self._finder(0, 0)
        self._finder(0, self.size - 7)
        self._finder(self.size - 7, 0)
        self._timing()
        points = _ALIGN[self.version]
        last = len(points) - 1
        for i, row in enumerate(points):
            for j, col in enumerate(points):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                self._alignment(row, col)
        self._reserve_format()
        self._reserve_version()
        self._draw_version()


def _mask(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _penalty(dark: list[list[bool]]) -> int:
    n = len(dark)
    score = 0
    for rows in (dark, [list(col) for col in zip(*dark)]):
        for line in rows:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
    for r in range(n - 1):
        for c in range(n - 1):
            v = dark[r][c]
            if dark[r + 1][c] == v and dark[r][c + 1] == v and dark[r + 1][c + 1] == v:
                score += 3
    patterns = ("10111010000", "00001011101")
    for rows in (dark, [list(col) for col in zip(*dark)]):
        for line in rows:
            text = "".join("1" if v else "0" for v in line)
            for start in range(n - 10):
                if text[start:start + 11] in patterns:
                    score += 40
    total = sum(1 for row in dark for v in row if v)
    percent = total * 100.0 / (n * n)
    score += int(abs(percent - 50) / 5) * 10
    return score


def make(text: str, mask: int | None = None) -> dict:
    """文本 -> ``{"version", "size", "mask", "rows": [[bool]]}``。"""
    data = str(text or "").encode("utf-8")
    version = _pick_version(len(data))
    codewords = _codewords(data, version)
    best: tuple[int, list[list[bool]]] | None = None
    masks = [mask] if mask is not None else list(range(8))
    for m in masks:
        matrix = _Matrix(version)
        matrix.draw()
        matrix._draw_format(m)
        matrix._place_data(codewords, m)
        score = _penalty(matrix.dark)
        if best is None or score < best[0]:
            best = (score, matrix.dark)
    assert best is not None
    return {"version": version, "size": len(best[1]),
            "mask": masks[0] if mask is not None else
            _best_mask(codewords, version, best[1]),
            "rows": best[1]}


def _best_mask(codewords: list[int], version: int,
               chosen: list[list[bool]]) -> int:
    # 返回与最终矩阵一致的掩码编号；重新算一遍比在循环里记更直观，
    # 代价只有 8 次小矩阵罚分，链接场景可以忽略。
    for m in range(8):
        matrix = _Matrix(version)
        matrix.draw()
        matrix._draw_format(m)
        matrix._place_data(codewords, m)
        if matrix.dark == chosen:
            return m
    return 0


def text_rows(text: str) -> dict:
    """给 HTTP 层用：把布尔矩阵压成 ``"0101"`` 字符串行。"""
    result = make(text)
    return {
        "version": result["version"],
        "size": result["size"],
        "mask": result["mask"],
        "rows": ["".join("1" if v else "0" for v in row)
                 for row in result["rows"]],
    }

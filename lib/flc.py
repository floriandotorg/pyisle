import io
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)

HEADER_SIZE = 8
CHUNK_HEADER_SIZE = 14


class FLC:
    # spell-checker: ignore PSTAMP BRUN MLEV USERSTRING LABELEX PATHMAP
    class ChunkType(IntEnum):
        CEL_DATA = 3
        COLOR_256 = 4
        DELTA_FLC = 7
        COLOR_64 = 11
        DELTA_FLI = 12
        BLACK = 13
        BYTE_RUN = 15
        FLI_COPY = 16
        PSTAMP = 18
        DTA_BRUN = 25
        DTA_COPY = 26
        DTA_LC = 27
        LABEL = 31
        BMP_MASK = 32
        MLEV_MASK = 33
        SEGMENT = 34
        KEY_IMAGE = 35
        KEY_PAL = 36
        REGION = 37
        WAVE = 38
        USERSTRING = 39
        RGN_MASK = 40
        LABELEX = 41
        SHIFT = 42
        PATHMAP = 43
        PREFIX_TYPE = 0xF100
        SCRIPT_CHUNK = 0xF1E0
        FRAME_TYPE = 0xF1FA
        SEGMENT_TABLE = 0xF1FB
        HUFFMAN_TABLE = 0xF1FC

    @dataclass
    class Color:
        r: int
        g: int
        b: int

        def __bytes__(self) -> bytes:
            return struct.pack("<BBB", self.r, self.g, self.b)

    def __init__(self, file: io.BufferedIOBase):
        self._file = file
        self._frames: list[bytes] = []
        self._palette: list[FLC.Color] = [FLC.Color(0, 0, 0)] * 256
        size, type, frames, self._width, self._height, self._delay_ms = struct.unpack("<IHHHH4xI108x", self._file.read(128))
        logger.debug(f"{size=:x} {type=:x} {frames=} {self._width=} {self._height=}")
        if type != 0xAF12:
            raise ValueError(f"Invalid FLC file: {type:x}")
        for _ in range(frames):
            self._read_chunk()

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def frames(self) -> list[bytes]:
        return self._frames

    @property
    def fps(self) -> int:
        return 1000 // self._delay_ms

    def _read_chunk(self) -> None:
        chunk_size, chunk_type = struct.unpack("<IH", self._file.read(6))
        end = self._file.tell() + chunk_size - 6
        logger.debug(f"{FLC.ChunkType(chunk_type).name=}")
        if chunk_type == FLC.ChunkType.FRAME_TYPE:
            chunks, must_be_zero = struct.unpack("<H8s", self._file.read(10))
            if must_be_zero != b"\x00\x00\x00\x00\x00\x00\x00\x00":
                raise ValueError(f"Unsupported settings: {must_be_zero}")
            if chunks == 0:
                self._frames.append(self._frames[-1])
                return
            for _ in range(chunks):
                self._read_chunk()
        elif chunk_type == FLC.ChunkType.COLOR_256 or chunk_type == FLC.ChunkType.COLOR_64:
            packets = struct.unpack("<H", self._file.read(2))[0]
            n = 0
            for _ in range(packets):
                skip, count = struct.unpack("<BB", self._file.read(2))
                n += skip
                if count == 0:
                    count = 256
                for _ in range(count):
                    self._palette[n] = FLC.Color(*struct.unpack("<BBB", self._file.read(3)))
                    n += 1
        elif chunk_type == FLC.ChunkType.BYTE_RUN:
            frame = bytearray()
            for _ in range(self._height):
                pixels = 0
                self._file.seek(1, io.SEEK_CUR)
                while pixels < self._width:
                    count = struct.unpack("<b", self._file.read(1))[0]
                    if count == 0:
                        raise ValueError("Invalid FLC file: count is 0")
                    elif count < 0:
                        frame.extend(b"".join(bytes(self._palette[byte]) for byte in self._file.read(-count)))
                    else:
                        frame.extend(bytes(self._palette[struct.unpack("<B", self._file.read(1))[0]]) * count)
                    pixels += abs(count)
            if len(frame) != self._width * self._height * 3:
                raise ValueError(f"Error: frame length mismatch {len(frame)}")
            self._frames.append(frame)
        elif chunk_type == FLC.ChunkType.DELTA_FLC:
            frame = bytearray(self._frames[-1])
            lines = struct.unpack("<H", self._file.read(2))[0]
            line = 0
            for _ in range(lines):
                pixel = 0
                while True:
                    opcode = struct.unpack("<H", self._file.read(2))[0]
                    code = opcode >> 14
                    if code == 0b00:
                        packets = opcode
                        break
                    elif code == 0b10:
                        pos = (line * self._width + self._width - 1) * 3
                        frame[pos : pos + 3] = bytes(self._palette[opcode & 0xFF])
                    elif code == 0b11:
                        line -= opcode - 2**16
                    else:
                        raise ValueError("Invalid FLC file: undefined opcode")
                for _ in range(packets):
                    skip, count = struct.unpack("<Bb", self._file.read(2))
                    pixel += skip
                    if count < 0:
                        p1, p2 = struct.unpack("<BB", self._file.read(2))
                        pos = (line * self._width + pixel) * 3
                        frame[pos : pos + 6 * (-count)] = (bytes(self._palette[p1]) + bytes(self._palette[p2])) * (-count)
                        pixel += 2 * (-count)
                    elif count > 0:
                        p = self._file.read(count * 2)
                        pos_start = (line * self._width + pixel) * 3
                        frame[pos_start : pos_start + 6 * count] = b''.join(
                            bytes(self._palette[p[i]]) + bytes(self._palette[p[i + 1]]) 
                            for i in range(0, count * 2, 2)
                        )
                        pixel += count * 2
                    else:
                        raise ValueError("Invalid FLC file: count is 0")
                line += 1
            if len(frame) != self._width * self._height * 3:
                raise ValueError(f"Error: frame length mismatch {len(frame)}")
            self._frames.append(frame)
        elif chunk_type == FLC.ChunkType.PSTAMP:
            self._file.seek(chunk_size - 6, io.SEEK_CUR)
        elif chunk_type == FLC.ChunkType.FLI_COPY:
            frame = bytearray()
            for pixel in self._file.read(self._width * self._height):
                frame.extend(bytes(self._palette[pixel]))
            self._frames.append(frame)
        elif chunk_type == FLC.ChunkType.BLACK:
            self._frames.append(b"\x00\x00\x00" * self._width * self._height)
        else:
            raise ValueError(f"Unsupported chunk type: {FLC.ChunkType(chunk_type).name}")
        if self._file.tell() < end:
            self._file.seek(end)

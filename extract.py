import io
import json
import logging
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from enum import IntEnum
from multiprocessing import Pool
from tkinter import filedialog
from typing import Any, BinaryIO, Union

from lib.animation import AnimationNode
from lib.flc import FLC
from lib.iso import ISO9660
from lib.si import SI
from lib.smk import SMK
from lib.wdb import WDB

logger = logging.getLogger(__name__)


class ColorSpace(IntEnum):
    RGB = 2
    RGBA = 6


def write_png(width: int, height: int, data: bytes, color: ColorSpace, stream: BinaryIO):
    def write_chunk(tag, data):
        stream.write(struct.pack(">I", len(data)))
        stream.write(tag)
        stream.write(data)
        stream.write(struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    # PNG file signature
    stream.write(b"\x89PNG\r\n\x1a\n")

    # IHDR chunk
    ihdr = struct.pack(">IIBBBBB", width, height, 8, int(color), 0, 0, 0)
    write_chunk(b"IHDR", ihdr)

    # Prepare raw image data (add filter byte 0 at start of each row)
    match color:
        case ColorSpace.RGB:
            byte_per_pixel = 3
        case ColorSpace.RGBA:
            byte_per_pixel = 4
        case _:
            raise ValueError(f"Invalid value for parameter color: {color}")

    if len(data) != width * height * byte_per_pixel:
        raise ValueError(f"Expected {width * height * byte_per_pixel} bytes but got {len(data)}")

    raw = b""
    stride = width * byte_per_pixel
    for y in range(height):
        raw += b"\x00" + data[y * stride : (y + 1) * stride]

    # IDAT chunk (compressed image data)
    compressed = zlib.compress(raw)
    write_chunk(b"IDAT", compressed)

    # IEND chunk
    write_chunk(b"IEND", b"")


class GLBWriter:
    USHORT = 5123
    FLOAT = 5126
    ARRAY_BUFFER = 34962
    ELEMENT_ARRAY_BUFFER = ARRAY_BUFFER + 1

    def __init__(self) -> None:
        self._bin_chunk_data = bytearray()
        self._buffer_views: list[dict] = []
        self._accessors: list[dict[str, Any]] = []

        self._json_textures: list[dict] = []
        self._json_images: list[dict] = []
        self._json_meshes: list[dict] = []
        self._json_materials: list[dict] = []

        self._textures: list[tuple[int, WDB.Gif]] = []

        self._nodes: list[dict[str, Any]] = []

    @staticmethod
    def _extend_gltf_chunk(type: bytes, content: bytes) -> bytes:
        result = bytearray()
        result.extend(struct.pack("<I4s", len(content), type))
        result.extend(content)
        return bytes(result)

    def add_node(self, parent_children: (list[int] | None) = None) -> dict[str, Any]:
        if parent_children is not None:
            if not self._nodes:
                raise Exception("Parent defined for first node")
            parent_children.append(len(self._nodes))
        elif self._nodes:
            raise Exception("No parent defined for further nodes")

        node: dict[str, Any] = {}
        self._nodes.append(node)
        return node

    def add_parent(self, name: str, parent_children: (list[int] | None) = None) -> dict[str, Any]:
        node = self.add_node(parent_children)
        children: list[int] = []
        node['name'] = name
        node['children'] = children
        return node

    def _append_bin_chunk(self, data: bytes, target: int | None) -> int:
        buffer_view_offset = len(self._bin_chunk_data)
        self._bin_chunk_data.extend(data)
        length = len(self._bin_chunk_data) - buffer_view_offset
        while len(self._bin_chunk_data) % 4:
            self._bin_chunk_data.append(0)
        buffer_view_index = len(self._buffer_views)
        buffer_view = {"buffer": 0, "byteOffset": buffer_view_offset, "byteLength": length}
        if target is not None:
            buffer_view["target"] = target
        self._buffer_views.append(buffer_view)
        return buffer_view_index

    def _extend_bin_chunk(self, fmt: str, data: list, target: int | None, componentType: int, type: str) -> int:
        chunk_data = bytearray()
        for entry in data:
            if not isinstance(entry, tuple):
                entry = (entry,)
            chunk_data.extend(struct.pack(fmt, *entry))
        buffer_view_index = self._append_bin_chunk(chunk_data, target)
        self._accessors.append({"bufferView": buffer_view_index, "componentType": componentType, "count": len(data), "type": type})
        return buffer_view_index

    def add_mesh(self, mesh: WDB.Mesh, texture: (WDB.Gif | None), name: str, children: (list[int] | None)):
        mesh_node = self.add_node(children)
        mesh_node['mesh'] = len(self._json_meshes)

        vertex_index = self._extend_bin_chunk("<fff", mesh.vertices, GLBWriter.ARRAY_BUFFER, GLBWriter.FLOAT, "VEC3")
        min_vertex = [min(vertex[axis] for vertex in mesh.vertices) for axis in range(0, 3)]
        max_vertex = [max(vertex[axis] for vertex in mesh.vertices) for axis in range(0, 3)]
        self._accessors[-1].update(
            {
                "min": min_vertex,
                "max": max_vertex,
            }
        )
        normal_index = self._extend_bin_chunk("<fff", mesh.normals, GLBWriter.ARRAY_BUFFER, GLBWriter.FLOAT, "VEC3")
        index_index = self._extend_bin_chunk("<H", mesh.indices, GLBWriter.ELEMENT_ARRAY_BUFFER, GLBWriter.USHORT, "SCALAR")

        json_mesh_data: dict[str, Any] = {
            "primitives": [
                {
                    "attributes": {
                        "POSITION": vertex_index,
                        "NORMAL": normal_index,
                    },
                    "indices": index_index,
                    "material": len(self._json_materials),
                }
            ],
            "name": name,
        }
        json_material = {"pbrMetallicRoughness": {"baseColorFactor": [mesh.color.red / 255, mesh.color.green / 255, mesh.color.blue / 255, 1 - mesh.color.alpha]}}
        self._json_meshes.append(json_mesh_data)
        self._json_materials.append(json_material)
        if mesh.uvs:
            uv_index = self._extend_bin_chunk("<ff", mesh.uvs, GLBWriter.ARRAY_BUFFER, GLBWriter.FLOAT, "VEC2")
            json_mesh_data["primitives"][0]["attributes"]["TEXCOORD_0"] = uv_index
        else:
            assert not mesh.texture_name

        if texture:
            mesh_index = len(self._json_meshes) - 1
            self._textures.append((mesh_index, texture))

    def _write_textures(self):
        for mesh_index, texture in self._textures:
            with io.BytesIO() as texture_file:
                write_png(texture.width, texture.height, texture.image, ColorSpace.RGB, texture_file)
                texture_data = texture_file.getvalue()
            texture_index = self._append_bin_chunk(texture_data, None)
            self._json_materials[mesh_index]["pbrMetallicRoughness"] = {"baseColorTexture": {"index": len(self._json_textures)}}
            self._json_textures.append({"source": len(self._json_images)})
            self._json_images.append({"mimeType": "image/png", "bufferView": texture_index})

    def build(self) -> bytearray:
        """Builds the glb file and returns the contents. Important: Adding meshes after calling this is not supported."""
        self._write_textures()
        self._textures.clear()

        json_data = {
            "asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(self._bin_chunk_data)}],
            "bufferViews": self._buffer_views,
            "accessors": self._accessors,
            "meshes": self._json_meshes,
            "materials": self._json_materials,
            "nodes": self._nodes,
            "scenes": [{"nodes": [0]}],
            "scene": 0,
        }

        if self._json_images:
            json_data["images"] = self._json_images
        if self._json_textures:
            json_data["textures"] = self._json_textures

        json_chunk_data = bytearray(json.dumps(json_data).encode("utf8"))
        while len(json_chunk_data) % 4:
            json_chunk_data.extend(b" ")

        contents = bytearray()
        contents.extend(GLBWriter._extend_gltf_chunk(b"JSON", json_chunk_data))
        contents.extend(GLBWriter._extend_gltf_chunk(b"BIN\0", self._bin_chunk_data))
        return contents

    def write(self, filename: str) -> None:
        """Writes the glb file. Important: Adding meshes after calling this is not supported."""
        contents = self.build()
        with open(filename, "wb") as file:
            file.write(struct.pack("<4sII", b"glTF", 2, 4 * 3 + len(contents)))
            file.write(contents)


def write_gltf2_mesh(mesh: WDB.Mesh, texture: (WDB.Gif | None), name: str, filename: str) -> None:
    writer = GLBWriter()
    writer.add_mesh(mesh, texture, name, None)
    writer.write(filename)


def write_gltf2_lod(wdb: WDB, lod: WDB.Lod, lod_name: str, filename: str) -> None:
    if lod.meshes:
        writer = GLBWriter()
        root = writer.add_parent(lod_name)
        children: list[int] = root["children"]
        for mesh_index, mesh in enumerate(lod.meshes):
            if mesh.uvs:
                texture = wdb.texture_by_name(mesh.texture_name)
            else:
                texture = None
            writer.add_mesh(mesh, texture, f"{lod_name}_M{mesh_index}", children)
        writer.write(filename)


def write_gltf2_model(wdb: WDB, model: WDB.Model, filename: str, all_lods: bool) -> None:
    writer = GLBWriter()

    def add_lod(lod_index: int, lod: WDB.Lod, name: str, children: list[int]):
        lod_name = f"{name}_L{lod_index}"
        lod_node = writer.add_parent(lod_name, children)
        for mesh_index, mesh in enumerate(lod.meshes):
            if mesh.uvs:
                texture = wdb.texture_by_name(mesh.texture_name)
            else:
                texture = None
            writer.add_mesh(mesh, texture, f"{lod_name}_M{mesh_index}", lod_node["children"])

    def add_roi(roi: WDB.Roi, animation: (AnimationNode | None), parent_node: dict[str, Any]) -> None:
        children: list[int] = parent_node["children"]

        transformation: dict[str, Any] = {}
        if animation:
            if animation.translation_keys:
                if len(animation.translation_keys) > 1:
                    logger.warning(f"Found {len(animation.translation_keys)} translations for {roi.name}")
                if animation.translation_keys[0].time != 0:
                    logger.warning(f"First translation key for {roi.name} is not at time 0")
                else:
                    # TODO: What to do with flags
                    transformation["translation"] = animation.translation_keys[0].vertex
            if animation.rotation_keys:
                if len(animation.rotation_keys) > 1:
                    logger.warning(f"Found {len(animation.rotation_keys)} rotations for {roi.name}")
                if animation.rotation_keys[0].time != 0:
                    logger.warning(f"First rotation key for {roi.name} is not at time 0")
                else:
                    # TODO: What to do with flags and time
                    transformation["rotation"] = animation.rotation_keys[0].quaternion
        parent_node.update(transformation)

        if all_lods:
            for lod_index, lod in enumerate(roi.lods):
                add_lod(lod_index, lod, roi.name, children)
        elif roi.lods:
            add_lod(len(roi.lods) - 1, roi.lods[-1], roi.name, children)

        for child in roi.children:
            child_node = writer.add_parent(child.name, children)
            if animation:
                child_animation = [x for x in animation.children if x.name.lower() == child.name.lower()]
                if len(child_animation) > 1:
                    logger.warning(f"Found {len(child_animation)} animations for {child.name}, using first")
            else:
                child_animation = []
            add_roi(child, child_animation[0] if child_animation else None, child_node)

    root = writer.add_parent(model.roi.name)
    add_roi(model.roi, model.animation, root)

    writer.write(filename)


def _export_wdb_roi(wdb: WDB, roi: WDB.Roi, root_name: str, prefix: str) -> int:
    prefix = f"{prefix}{roi.name}"
    result = 0
    for lod_index, lod in enumerate(roi.lods):
        lod_name = f"{prefix}_L{lod_index}"
        for mesh_index, mesh in enumerate(lod.meshes):
            if mesh.texture_name != "":
                texture = wdb.texture_by_name(mesh.texture_name)
            else:
                texture = None
            assert (texture is not None) == bool(mesh.uvs), f"{texture=} == {len(mesh.uvs)}; {texture is not None=}; {bool(mesh.uvs)=}"
            mesh_name = f"{lod_name}_M{mesh_index}"
            write_gltf2_mesh(mesh, texture, mesh_name, f"extract/WORLD.WDB/{root_name}/parts/{mesh_name}.glb")
            result += 1
        write_gltf2_lod(wdb, lod, lod_name, f"extract/WORLD.WDB/{root_name}/parts/{lod_name}.glb")
        result += 1
    for child in roi.children:
        _export_wdb_roi(wdb, child, root_name, f"{prefix}_R")
    return result


def export_wdb_model(wdb: WDB, model: WDB.Model) -> int:
    file_count = 0
    os.makedirs(f"extract/WORLD.WDB/{model.roi.name}/parts", exist_ok=True)
    file_count += _export_wdb_roi(wdb, model.roi, model.roi.name, "")
    write_gltf2_model(wdb, model, f"extract/WORLD.WDB/{model.roi.name}/model.glb", False)
    file_count += 1
    write_gltf2_model(wdb, model, f"extract/WORLD.WDB/{model.roi.name}/all_lods.glb", True)
    file_count += 1
    return file_count


def write_bitmap(filename: str, obj: SI.Object) -> None:
    with open(filename, "wb") as file:
        file.write(struct.pack("<2sIII", b"BM", len(obj.data), 0, obj.chunk_sizes[0] + 14))
        file.write(obj.data)


def write_flc(dest_file: io.BufferedIOBase, obj: SI.Object) -> None:
    src_file = obj.open()
    for n, chunk_size in enumerate(obj.chunk_sizes):
        chunk = src_file.read(chunk_size)
        if n == 0:
            dest_file.write(chunk)
            continue
        if chunk_size == 20:
            dest_file.write(b"\x10\x00\x00\x00\xfa\xf1\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
            continue
        dest_file.write(chunk[20:])


def write_gif(gif: WDB.Gif, filename: str) -> None:
    with open(filename, "wb") as file:
        width = gif.width
        height = gif.height
        pad = b"\x00" * ((4 - (width * 3) % 4) % 4)
        header_size = 54
        bf_size = header_size + (width * 3 + len(pad)) * height
        bi_size = bf_size - header_size

        # BMP Header (14 bytes)
        file.write(struct.pack("<2sIHHI", b"BM", bf_size, 0, 0, header_size))

        # DIB Header (40 bytes)
        file.write(
            struct.pack(
                "<IIiHHIIIIII",
                40,  # DIB header size
                width,  # Width
                -height,  # Height
                1,  # Color planes
                24,  # Bits per pixel (RGB = 24)
                0,  # No compression
                bi_size,  # Image size
                0,  # Horizontal resolution (pixels/meter)
                0,  # Vertical resolution (pixels/meter)
                0,  # Number of colors in palette
                0,  # Important colors
            )
        )

        bgr_frame = bytearray(len(gif.image))
        bf = memoryview(bgr_frame)
        rf = memoryview(gif.image)
        # Swap R and B:
        bf[0::3] = rf[2::3]  # B
        bf[1::3] = rf[1::3]  # G
        bf[2::3] = rf[0::3]  # R

        if pad:
            row_size = width * 3
            file.write(b"".join(bgr_frame[i : i + row_size] + pad for i in range(0, len(bgr_frame), row_size)))
        else:
            file.write(bgr_frame)


def write_flc_sprite_sheet(flc: FLC, filename: str) -> None:
    with open(filename, "wb") as file:
        width = flc.width
        height = flc.height * len(flc.frames)
        pad = b"\x00" * ((4 - (width * 3) % 4) % 4)
        header_size = 54
        bf_size = header_size + (width * 3 + len(pad)) * height
        bi_size = bf_size - header_size

        # BMP Header (14 bytes)
        file.write(struct.pack("<2sIHHI", b"BM", bf_size, 0, 0, header_size))

        # DIB Header (40 bytes)
        file.write(
            struct.pack(
                "<IIiHHIIIIII",
                40,  # DIB header size
                width,  # Width
                -height,  # Height
                1,  # Color planes
                24,  # Bits per pixel (RGB = 24)
                0,  # No compression
                bi_size,  # Image size
                0,  # Horizontal resolution (pixels/meter)
                0,  # Vertical resolution (pixels/meter)
                0,  # Number of colors in palette
                0,  # Important colors
            )
        )

        for frame in flc.frames:
            bgr_frame = bytearray(len(frame))
            bf = memoryview(bgr_frame)
            rf = memoryview(frame)
            # Swap R and B:
            bf[0::3] = rf[2::3]  # B
            bf[1::3] = rf[1::3]  # G
            bf[2::3] = rf[0::3]  # R

            if pad:
                row_size = width * 3
                file.write(b"".join(bgr_frame[i : i + row_size] + pad for i in range(0, len(bgr_frame), row_size)))
            else:
                file.write(bgr_frame)


def write_smk_avi(video: Union[SMK, FLC], filename: str) -> None:
    with open(filename, "wb") as file:
        pad = b"\x00" * ((4 - (video.width * 3) % 4) % 4)
        total_frame_size = (video.width + len(pad)) * video.height * 3

        file.write(
            struct.pack(
                "<4sI4s4sI4s4sIIIIIIIIIII16x4sI4s4sI4s4sIIIIIIIIIIII4sIIIiHHIIIIII",
                b"RIFF",  # RIFF signature
                0,  # File size (filled later)
                b"AVI ",  # AVI signature
                b"LIST",
                4 + 64 + 124,  # Size of LIST chunk
                b"hdrl",
                b"avih",
                56,  # Size of avih chunk
                1_000_000 // int(video.fps),  # Microseconds per frame
                total_frame_size,  # Max bytes per second
                1,  # Padding granularity
                0,  # Flags
                len(video.frames),  # Total frames
                0,  # Initial frames
                1,  # Number of streams
                total_frame_size,  # Suggested buffer size
                video.width,  # Width
                video.height,  # Height
                b"LIST",
                116,
                b"strl",
                b"strh",
                56,
                b"vids",  # Video stream type
                b"DIB ",  # Video codec (uncompressed)
                0,  # Flags
                0,  # Priority + Language
                0,  # Initial frames
                1,  # Scale
                int(video.fps),  # Rate
                0,  # Start
                len(video.frames),  # Length
                total_frame_size,  # Suggested buffer size
                0,  # Quality
                total_frame_size,  # Sample size
                0,  # rcFrame
                0,  # rcFrame: right, bottom
                b"strf",
                40,
                40,
                video.width,  # Width
                -video.height,  # Height (negative for top-down)
                1,  # Color planes
                24,  # Bits per pixel (RGB = 24)
                0,  # No compression
                total_frame_size,  # Image size
                0,  # Horizontal resolution (pixels/meter)
                0,  # Vertical resolution (pixels/meter)
                0,  # Number of colors in palette
                0,  # Important colors
            )
        )

        file.write(
            struct.pack(
                "<4sI4s",
                b"LIST",
                len(video.frames) * (total_frame_size + 8) + 4,
                b"movi",
            )
        )

        for frame in video.frames:
            file.write(
                struct.pack(
                    "<4sI",
                    b"00db",
                    total_frame_size,
                )
            )

            bgr_frame = bytearray(len(frame))
            bf = memoryview(bgr_frame)
            rf = memoryview(frame)
            # Swap R and B:
            bf[0::3] = rf[2::3]  # B
            bf[1::3] = rf[1::3]  # G
            bf[2::3] = rf[0::3]  # R

            if pad:
                row_size = video.width * 3
                file.write(b"".join(bgr_frame[i : i + row_size] + pad for i in range(0, len(bgr_frame), row_size)))
            else:
                file.write(bgr_frame)

        file_size = file.tell()
        file.seek(4, io.SEEK_SET)
        file.write(struct.pack("<I", file_size - 8))


def write_si(filename: str, obj: SI.Object) -> bool:
    os.makedirs(f"extract/{filename}", exist_ok=True)

    match obj.file_type:
        case SI.FileType.WAV:

            def extend_wav_chunk(type: bytes, content: bytes) -> bytes:
                result = bytearray()
                result.extend(struct.pack("<4sI", type, len(content)))
                result.extend(content)
                if (len(content) % 2) == 1:
                    result.append(0)
                return bytes(result)

            with open(f"extract/{filename}/{obj.id}.wav", "wb") as file:
                content = bytearray()
                content.extend(b"WAVE")
                content.extend(extend_wav_chunk(b"fmt ", obj.data[: obj.chunk_sizes[0]]))
                content.extend(extend_wav_chunk(b"data", obj.data[obj.chunk_sizes[0] :]))
                file.write(extend_wav_chunk(b"RIFF", content))
            return True
        case SI.FileType.STL:
            write_bitmap(f"extract/{filename}/{obj.id}.bmp", obj)
        case SI.FileType.FLC:
            mem_file = io.BytesIO()
            write_flc(mem_file, obj)
            mem_file.seek(0)
            with open(f"extract/{filename}/{obj.id}.flc", "wb") as file:
                file.write(mem_file.getvalue())
            mem_file.seek(0)
            try:
                flc = FLC(mem_file)
                write_flc_sprite_sheet(flc, f"extract/{filename}/{obj.id}_frames{len(flc.frames)}_fps{flc.fps}.bmp")
                write_smk_avi(flc, f"extract/{filename}/{obj.id}.avi")
            except Exception as e:
                logger.error(f"Error writing {filename}_{obj.id}.flc: {e}")
                return False
            return True
        case SI.FileType.SMK:
            with open(f"extract/{filename}/{obj.id}.smk", "wb") as file:
                file.write(obj.data)
            smk = SMK(io.BytesIO(obj.data))
            write_smk_avi(smk, f"extract/{filename}/{obj.id}.avi")
            return True
    return False


def get_iso_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]

    path = filedialog.askopenfilename(
        title="Select ISO file",
        filetypes=[("ISO files", "*.iso"), ("All files", "*.*")],
    )
    if not path:
        sys.exit("No file selected")
    return path


@dataclass
class File:
    si: SI
    name: str
    weight: int

    def _obj_weight(self, obj: SI.Object) -> int:
        match obj.file_type:
            case SI.FileType.FLC:
                frames, width, height = struct.unpack("<6xHHH", obj.data[0:12])
                return (width * height * frames) / 10_000
            case SI.FileType.SMK:
                width, height, frames = struct.unpack("<4xIII", obj.data[0:16])
                return (width * height * frames) / 2_000
            case _:
                return 10

    def __init__(self, si: SI, name: str):
        self.si = si
        self.name = name
        self.weight = sum(self._obj_weight(obj) for obj in self.si.object_list.values())

    def __hash__(self) -> int:
        return hash(self.name)


def process_file(file: File) -> int:
    logger.info(f"Extracting {file.name} ..")
    result = sum(1 if write_si(os.path.basename(file.name), obj) else 0 for obj in file.si.object_list.values())
    logger.info(f"Extracting {file.name} .. [done]")
    return result


def process_files(files: list[File]) -> int:
    return sum(process_file(file) for file in files)


def balanced_chunks(data: list[File], n: int) -> list[list[File]]:
    data = sorted(data, key=lambda x: x.weight, reverse=True)
    chunks: list[list[File]] = [[] for _ in range(n)]
    sums = [0] * n
    for item in data:
        i = sums.index(min(sums))
        chunks[i].append(item)
        sums[i] += item.weight
    return chunks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    os.makedirs("extract", exist_ok=True)

    si_files: list[File] = []
    wdb_files: list[io.BytesIO] = []
    with ISO9660(get_iso_path()) as iso:
        for file in iso.filelist:
            if not file.endswith(".SI") and not file.endswith(".WDB"):
                continue

            try:
                mem_file = io.BytesIO()
                mem_file.write(iso.open(file).read())
                mem_file.seek(0, io.SEEK_SET)
                if file.endswith(".SI"):
                    si_files.append(File(SI(mem_file), file))
                elif file.endswith(".WDB"):
                    wdb_files.append(mem_file)
                else:
                    raise ValueError(f"Unknown file type: {file}")
            except ValueError:
                logger.error(f"Error opening {file}")

    cpus = os.cpu_count()
    if cpus is None:
        cpus = 1

    exported_files = 0
    with Pool(processes=cpus) as pool:
        results = pool.map_async(process_files, balanced_chunks(si_files, cpus))

        logger.info("Exporting WDB models ..")
        os.makedirs("extract/WORLD.WDB", exist_ok=True)
        for wdb_file in wdb_files:
            wdb = WDB(wdb_file)
            for model in wdb.models:
                exported_files += export_wdb_model(wdb, model)
            for image in wdb.images:
                write_gif(image, f"extract/WORLD.WDB/{model.roi.name}_{image.title}.bmp")
            for texture in wdb.textures:
                write_gif(texture, f"extract/WORLD.WDB/{model.roi.name}_{texture.title}.bmp")
            for model_texture in wdb.model_textures:
                write_gif(model_texture, f"extract/WORLD.WDB/{model.roi.name}_{model_texture.title}.bmp")
        logger.info("Exporting WDB models .. [done]")

        exported_files += sum(results.get())

    logger.info(f"Exported {exported_files} files")

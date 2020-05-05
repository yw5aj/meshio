"""
I/O for FLAC3D format.
"""
import logging
import struct
import time

import numpy

from ..__about__ import __version__ as version
from .._common import _pick_first_int_data
from .._exceptions import ReadError, WriteError
from .._files import open_file
from .._helpers import register
from .._mesh import Mesh

meshio_only = {
    "tetra": "tetra",
    "tetra10": "tetra",
    "pyramid": "pyramid",
    "pyramid13": "pyramid",
    "wedge": "wedge",
    "wedge12": "wedge",
    "wedge15": "wedge",
    "wedge18": "wedge",
    "hexahedron": "hexahedron",
    "hexahedron20": "hexahedron",
    "hexahedron24": "hexahedron",
    "hexahedron27": "hexahedron",
}


numnodes_to_meshio_type = {
    4: "tetra",
    5: "pyramid",
    6: "wedge",
    8: "hexahedron",
}
meshio_type_to_numnodes = {v: k for k, v in numnodes_to_meshio_type.items()}


meshio_to_flac3d_type = {
    "tetra": "T4",
    "pyramid": "P5",
    "wedge": "W6",
    "hexahedron": "B8",
}


flac3d_to_meshio_order = {
    "tetra": [0, 1, 2, 3],
    "pyramid": [0, 1, 4, 2, 3],
    "wedge": [0, 1, 3, 2, 4, 5],
    "hexahedron": [0, 1, 4, 2, 3, 6, 7, 5],
}


meshio_to_flac3d_order = {
    "tetra": [0, 1, 2, 3],
    "pyramid": [0, 1, 3, 4, 2],
    "wedge": [0, 1, 3, 2, 4, 5],
    "hexahedron": [0, 1, 3, 4, 2, 7, 5, 6],
}


meshio_to_flac3d_order_2 = {
    "tetra": [0, 2, 1, 3],
    "pyramid": [0, 3, 1, 4, 2],
    "wedge": [0, 2, 3, 1, 5, 4],
    "hexahedron": [0, 3, 1, 4, 2, 5, 7, 6],
}


def read(filename):
    """
    Read FLAC3D f3grid grid file.
    """
    # Read a small block of the file to assess its type
    # See <http://code.activestate.com/recipes/173220/>
    with open_file(filename, "rb") as f:
        block = f.read(8)
        is_binary = b"\x00" in block

    with open_file(filename) as f:
        out = read_buffer(f, is_binary)

    return out


def read_buffer(f, is_binary):
    """
    Read binary or ASCII file.
    """
    points = []
    point_ids = {}
    cells = []
    mapper = {}
    field_data = {}
    slots = set()

    if is_binary:
        # Not sure what the first bytes represent, the format might be wrong
        # It does not seem to be useful anyway
        _ = numpy.fromfile(f, "u4", 2)

        # Points
        num_nodes, = numpy.fromfile(f, int, 1)
        for pidx in range(num_nodes):
            pid, point = _read_point(f, is_binary)
            points.append(point)
            point_ids[pid] = pidx

        # Cells
        num_cells, = numpy.fromfile(f, int, 1)
        for cidx in range(num_cells):
            cid, cell = _read_cell(f, point_ids, is_binary)
            cells = _update_cells(cells, cell)
            mapper[cid] = [cidx]

        # Zone groups
        num_groups, = numpy.fromfile(f, int, 1)
        for zidx in range(num_groups):
            name, slot, data = _read_zgroup(f, is_binary)
            field_data, mapper = _update_field_data(field_data, mapper, data, name, zidx + 1)
            slots = _update_slots(slots, slot)

    else:
        pidx = 0
        zidx = 0
        count = 0

        line = f.readline().rstrip().split()
        while line:
            if line[0] == "G":
                pid, point = _read_point(line, is_binary)
                points.append(point)
                point_ids[pid] = pidx
                pidx += 1
            elif line[0] == "Z":
                cid, cell = _read_cell(line, point_ids, is_binary)
                cells = _update_cells(cells, cell)
                mapper[cid] = [count]
                count += 1
            elif line[0] == "ZGROUP":
                name, slot, data = _read_zgroup(f, is_binary, line)
                field_data, mapper = _update_field_data(field_data, mapper, data, name, zidx + 1)
                slots = _update_slots(slots, slot)
                zidx += 1
            
            line = f.readline().rstrip().split()

    if field_data:
        num_cells = numpy.cumsum([len(c[1]) for c in cells])
        cell_data = numpy.empty(num_cells[-1], dtype=int)
        for cid, zid in mapper.values():
            cell_data[cid] = zid
        cell_data = {"flac3d:zone": numpy.split(cell_data, num_cells[:-1])}
    else:
        cell_data = {}

    return Mesh(
        points=numpy.array(points),
        cells=[(k, numpy.array(v)[:, flac3d_to_meshio_order[k]]) for k, v in cells],
        cell_data=cell_data,
        field_data=field_data,
    )


def _read_point(buf_or_line, is_binary):
    """
    Read point coordinates.
    """
    if is_binary:
        pid, = numpy.fromfile(buf_or_line, int, 1)
        point = numpy.fromfile(buf_or_line, float, 3)
    else:
        pid = int(buf_or_line[1])
        point = [float(l) for l in buf_or_line[2:]]
    
    return pid, point


def _read_cell(buf_or_line, point_ids, is_binary):
    """
    Read cell corners.
    """
    if is_binary:
        cid, num_verts = numpy.fromfile(buf_or_line, int, 2)
        cell = numpy.fromfile(buf_or_line, int, num_verts)
        is_b7 = num_verts == 7
    else:
        cid = int(buf_or_line[2])
        cell = buf_or_line[3:]
        is_b7 = buf_or_line[1] == "B7"
    
    cell = [point_ids[int(l)] for l in cell]
    if is_b7:
        cell.append(cell[-1])

    return cid, cell


def _read_zgroup(buf_or_line, is_binary, line=None):
    """
    Read cell group.
    """
    if is_binary:
        # Group name
        num_chars, = numpy.fromfile(buf_or_line, "u2", 1)
        name, = numpy.fromfile(buf_or_line, "|S{}".format(num_chars), 1).astype("|U{}".format(num_chars))

        # Slot name
        num_chars, = numpy.fromfile(buf_or_line, "u2", 1)
        slot, = numpy.fromfile(buf_or_line, "|S{}".format(num_chars), 1).astype("|U{}".format(num_chars))

        # Zones
        num_zones, = numpy.fromfile(buf_or_line, int, 1)
        data = numpy.fromfile(buf_or_line, int, num_zones)
    else:
        name = line[1].replace('"', "")
        data = []
        slot = "" if "SLOT" not in line else line[-1]

        i = buf_or_line.tell()
        line = buf_or_line.readline()
        while True:
            line = line.rstrip().split()
            if line and (line[0] not in {"*", "ZGROUP"}):
                data += [int(l) for l in line]
            else:
                buf_or_line.seek(i)
                break
            i = buf_or_line.tell()
            line = buf_or_line.readline()

    return name, slot, data


def _update_cells(cells, cell):
    """
    Update cell list.
    """
    cell_type = numnodes_to_meshio_type[len(cell)]
    if len(cells) > 0 and cell_type == cells[-1][0]:
        cells[-1][1].append(cell)
    else:
        cells.append((cell_type, [cell]))

    return cells


def _update_field_data(field_data, mapper, data, name, zidx):
    """
    Update field data dict.
    """
    for cid in data:
        mapper[cid].append(zidx)
    field_data[name] = numpy.array([zidx, 3])

    return field_data, mapper


def _update_slots(slots, slot):
    """
    Update slot set. Only one slot is supported.
    """
    slots.add(slot)
    if len(slots) > 1:
        raise ReadError("Multiple slots are not supported")
        
    return slots


def write(filename, mesh, float_fmt=".15e", binary=False):
    """
    Write FLAC3D f3grid grid file (only ASCII).
    """
    if not any(c.type in meshio_only.keys() for c in mesh.cells):
        raise WriteError("FLAC3D format only supports 3D cells")

    if not binary:
        with open_file(filename, "w") as f:
            f.write("* FLAC3D grid produced by meshio v{}\n".format(version))
            f.write("* {}\n".format(time.ctime()))
            f.write("* GRIDPOINTS\n")
            _write_points(f, mesh.points, float_fmt)
            f.write("* ZONES\n")
            _write_cells(f, mesh.points, mesh.cells)

            if mesh.cell_data:
                # pick out material
                key, other = _pick_first_int_data(mesh.cell_data)
                if key:
                    material = numpy.concatenate(mesh.cell_data[key])
                    if other:
                        logging.warning(
                            "FLAC3D can only write one cell data array. "
                            "Picking {}, skipping {}.".format(key, ", ".join(other))
                        )
                else:
                    material = None

                if material is not None:
                    f.write("* ZONE GROUPS\n")
                    zgroups, labels = _translate_zgroups(material, mesh.field_data)
                    for k in sorted(zgroups.keys()):
                        f.write('ZGROUP "{}"\n'.format(labels[k]))
                        _write_zgroup(f, zgroups[k])
    else:
        with open_file(filename, "wb") as f:
            # Don't know what these values represent, but it works
            numpy.array([1375135718, 3]).tofile(f)

            # Points
            f.write(struct.pack("I", len(mesh.points)))
            for i, point in enumerate(mesh.points):
                f.write(struct.pack("I", i + 1))
                point.tofile(f)

            # Cells
            zones = _translate_zones(mesh.points, mesh.cells)

            f.write(struct.pack("I", sum(len(c.data) for c in mesh.cells)))
            count = 0
            for _, zone in zones:
                num_cells, num_verts = zone.shape
                numpy.column_stack((
                    numpy.arange(1, num_cells + 1) + count,
                    numpy.full(num_cells, num_verts),
                    zone + 1,
                )).tofile(f)
                count += num_cells

            # Zone groups
            if mesh.cell_data:
                # pick out material
                key, other = _pick_first_int_data(mesh.cell_data)
                if key:
                    material = numpy.concatenate(mesh.cell_data[key])
                    if other:
                        logging.warning(
                            "FLAC3D can only write one cell data array. "
                            "Picking {}, skipping {}.".format(key, ", ".join(other))
                        )
                else:
                    material = None

            if material is not None:
                zgroups, labels = _translate_zgroups(material, mesh.field_data)
                f.write(struct.pack("I", len(zgroups)))
                for k in sorted(zgroups.keys()):
                    num_chars = len(labels[k])
                    numpy.array([num_chars], dtype="u2").tofile(f)
                    numpy.array([labels[k]], dtype="|S").tofile(f)
                    numpy.array([7], dtype="u2").tofile(f)
                    numpy.array(["Default"], dtype="|S").tofile(f)
                    f.write(struct.pack("I", len(zgroups[k])))
                    zgroups[k].astype(int).tofile(f)
            else:
                f.write(struct.pack("I", 0))

            # No face and face group
            f.write(struct.pack("2I", 0, 0))


def _write_points(f, points, float_fmt):
    """
    Write points coordinates.
    """
    for i, point in enumerate(points):
        fmt = "G\t{:8}\t" + "\t".join(3 * ["{:" + float_fmt + "}"]) + "\n"
        f.write(fmt.format(i + 1, *point))


def _write_cells(f, points, cells):
    """Write zones.
    """
    zones = _translate_zones(points, cells)
    i = 1
    for meshio_type, zone in zones:
        fmt = "Z {} {} " + " ".join(["{}"] * zone.shape[1]) + "\n"
        for entry in zone + 1:
            f.write(fmt.format(meshio_to_flac3d_type[meshio_type], i, *entry))
            i += 1


def _translate_zones(points, cells):
    """Reorder meshio cells to FLAC3D zones. Four first points must form a right-handed
    coordinate system (outward normal vectors). Reorder corner points according to sign
    of scalar triple products.
    """
    # See <https://stackoverflow.com/a/42386330/353337>
    def slicing_summing(a, b, c):
        c0 = b[:, 1] * c[:, 2] - b[:, 2] * c[:, 1]
        c1 = b[:, 2] * c[:, 0] - b[:, 0] * c[:, 2]
        c2 = b[:, 0] * c[:, 1] - b[:, 1] * c[:, 0]
        return a[:, 0] * c0 + a[:, 1] * c1 + a[:, 2] * c2

    zones = []
    for key, idx in cells:
        if key not in meshio_only.keys():
            continue

        # Compute scalar triple products
        key = meshio_only[key]
        tmp = points[idx[:, meshio_to_flac3d_order[key][:4]].T]
        det = slicing_summing(tmp[1] - tmp[0], tmp[2] - tmp[0], tmp[3] - tmp[0])
        # Reorder corner points
        data = numpy.where(
            (det > 0)[:, None],
            idx[:, meshio_to_flac3d_order[key]],
            idx[:, meshio_to_flac3d_order_2[key]],
        )
        zones.append((key, data))

    return zones


def _translate_zgroups(zone_data, field_data):
    """Convert meshio cell_data to FLAC3D zone groups.
    """
    zgroups = {k: numpy.nonzero(zone_data == k)[0] + 1 for k in numpy.unique(zone_data)}

    labels = {k: str(k) for k in zgroups.keys()}
    labels[0] = "None"
    if field_data:
        labels.update({v[0]: k for k, v in field_data.items() if v[1] == 3})
    return zgroups, labels


def _write_zgroup(f, data, ncol=20):
    """Write zone group data.
    """
    nrow = len(data) // ncol
    lines = numpy.split(data, numpy.full(nrow, ncol).cumsum())
    for line in lines:
        if len(line):
            f.write(" {}\n".format(" ".join([str(l) for l in line])))


register("flac3d", [".f3grid"], read, {"flac3d": write})

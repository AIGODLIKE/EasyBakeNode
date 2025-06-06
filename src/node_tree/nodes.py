from __future__ import annotations
from bpy.types import Node, Context, UILayout
from pathlib import Path
from functools import cache
from subprocess import Popen, PIPE, STDOUT
from tempfile import gettempdir
from ...utils.logger import logger
from ...utils.timeit import ScopeTimer
from ...utils.timer import Timer
from ...utils.shm import SHM
from .common import TreeCtx
from .executor import TaskExecutor
from collections.abc import Iterable

import bpy
import bpy.utils.previews
import json
import re
import numpy as np

TREE_TCTX = "BakeNodes"
NODE_TCTX = "BakeNode"

common_kwargs = {"translation_context": NODE_TCTX}
if bpy.app.version < (4, 0, 0):
    common_kwargs = {}


def find_from_node(socket: bpy.types.NodeSocket) -> NodeBase:
    if not socket.is_linked:
        return None
    node: bpy.types.Node = socket.links[0].from_node
    if node.bl_idname != "NodeReroute":
        return node
    return find_from_node(node.inputs[0])


def find_from_nodes(socket: bpy.types.NodeSocket) -> NodeBase:
    nodes = []
    if not socket.is_linked:
        return nodes
    for link in socket.links:
        node: bpy.types.Node = link.from_node
        if node.bl_idname != "NodeReroute":
            nodes.append(node)
        else:
            nodes.extend(find_from_nodes(node.inputs[0]))
    return nodes


def find_to_node(socket: bpy.types.NodeSocket) -> NodeBase:
    if not socket.is_linked:
        return None
    node: bpy.types.Node = socket.links[0].to_node
    if node.bl_idname != "NodeReroute":
        return node
    return find_to_node(node.outputs[0])


class NodeBase(Node):
    exclude = False
    category = "None"
    bl_label = "NodeBase"

    is_output: bpy.props.BoolProperty(default=False)
    is_dumped: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == "BakeNodeTree"

    @staticmethod
    @cache
    def get_node_cls(c) -> NodeBase:
        subclasses = NodeBase.__subclasses__()
        for sc in subclasses:
            subclasses.extend(sc.__subclasses__())
        for cls in subclasses:
            if cls.bl_label != c:
                continue
            return cls
        return NodeBase

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        executor.warn("%s [执行]", cls.nname)
        # if not args and not kwargs:
        #     executor.warn("%s [执行]", cls.nname)
        # elif args:
        #     executor.warn("%s [执行]: %s", cls.nname, args)
        # elif kwargs:
        #     executor.warn("%s [执行]: %s", cls.nname, kwargs)
        return {}

    def get_from_nodes(self, socket) -> list[NodeBase]:
        if socket.is_multi_input:
            return find_from_nodes(socket)
        node = find_from_node(socket)
        return [node]

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        for inp in self.inputs:
            nodes = self.get_from_nodes(inp)
            for node in nodes:
                if not node or node.is_dumped:
                    continue
                node.dump(ctx)
        self.is_dumped = True
        queue = ctx.ensure_list("ExecutionQueue")
        queue.append((self.bl_label, self.name))
        return ctx

    ICON_CACHE = bpy.utils.previews.new()

    @classmethod
    def get_icon(cls, cat, name):
        if name not in cls.ICON_CACHE:
            suffixes = [".png", ".jpg", ".jpeg"]
            for suffix in suffixes:
                icon_path = Path(__file__).parent.joinpath(f"icons/{cat}/{name}{suffix}")
                if icon_path.exists():
                    break
            if not icon_path.exists():
                return 0
            cls.ICON_CACHE.load(name, icon_path.as_posix(), 'IMAGE')
        return cls.ICON_CACHE[name].icon_id

    def draw_icon(self, layout: UILayout, icon: int):
        if not icon:
            return
        layout.template_icon(icon_value=icon, scale=7)


class Bake(NodeBase):
    category = "Core"
    bl_label = "Bake"
    bl_icon = "NODETREE"

    def bake_pass_items(_, __):
        def_items = [
            ("COMBINED", "Combined", "", "NONE", 0),
            ("AO", "Ambient Occlusion", "", "NONE", 1),
            ("SHADOW", "Shadow", "", "NONE", 2),
            ("POSITION", "Position", "", "NONE", 11),
            ("NORMAL", "Normal", "", "NONE", 3),
            ("UV", "UV", "", "NONE", 4),
            ("ROUGHNESS", "Roughness", "", "NONE", 5),
            ("EMIT", "Emit", "", "NONE", 6),
            ("ENVIRONMENT", "Environment", "", "NONE", 7),
            ("DIFFUSE", "Diffuse", "", "NONE", 8),
            ("GLOSSY", "Glossy", "", "NONE", 9),
            ("TRANSMISSION", "Transmission", "", "NONE", 10),
        ]
        if ct := getattr(bpy.context.scene, "cycles", None):
            btp = ct.bl_rna.properties["bake_type"].enum_items
            def_items = [(ei.identifier, ei.name, ei.description, ei.icon, ei.value) for ei in btp]
        return def_items

    bake_pass: bpy.props.EnumProperty(items=bake_pass_items, name="Bake Type")

    def init(self, context: Context):
        self.width = 250
        self.inputs.new("Pass", "Pass", use_multi_input=True)
        self.inputs.new("BakeSetting", "Bake Setting")
        self.inputs.new("Mesh", "Meshes", use_multi_input=True)
        self.outputs.new("Image", "Image")

    def draw_buttons(self, context: Context, layout: UILayout):
        if self.inputs["Pass"].is_linked:
            return
        layout.prop(self, "bake_pass")
        # for ei in enum_items: print((ei.identifier, ei.name, ei.description, ei.icon, ei.value))

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        super().execute(executor, task, *args, **kwargs)
        # ctx = TreeCtx().load(kwargs)
        ctx = task  # TODO 改进
        # bake_settings = ctx.get("BakeSettings", {})
        # out_images = ctx.ensure_dict("OutImages")
        meshes = ctx.get("Meshes", [])
        bake_settings = ctx.get("BakeSettings", {})
        bake_passes = ctx.get("Pass", {})

        bake_queue = []
        data = set()
        for mesh_pair in meshes:
            dst, src, uv = mesh_pair
            if dst not in bpy.data.objects:
                continue
            data.add(bpy.data.objects[dst])
            if src in bpy.data.objects:
                data.add(bpy.data.objects[src])
            for cat, passes in bake_passes.items():
                for bake_pass in passes:
                    bake_queue.append((tuple(mesh_pair), cat, bake_pass))
        # 保存blend文件
        blend_path = Path(bpy.app.tempdir).joinpath(f"BAKE_NODE_{cls.nname}.blend")

        @Timer.wait_run
        def f():
            bpy.ops.wm.save_as_mainfile(filepath=blend_path.as_posix(), copy=True)

        f()

        # 后台进程blender 运行 blend文件
        blender = bpy.app.binary_path
        cwd = Path(__file__).parent
        args = [blender]
        args.append(blend_path.as_posix())
        args.append("-b")
        args.append("-P")
        args.append(cwd.joinpath("run.py").as_posix())
        args.append("--factory-startup")
        args.append("--")
        args.append("-bnc")
        res = bake_settings.get("resolution", (512, 512))
        # sm = SHM.create2("BakeNodeSHM", res[0] * res[1] * 4 * 4)
        sm = SHM.create(res[0] * res[1] * 4 * 4)
        pixels = np.ndarray((*res, 4), dtype=np.float32, buffer=sm.buf)
        pixels.fill(0)
        args.append("{}")
        out_images = ctx.ensure_dict("OutImages")
        run_params = {}
        for i, (mesh_pair, cat, bake_pass) in enumerate(bake_queue):
            config = {
                "ctx": ctx,
                "bake_params": (mesh_pair, cat, bake_pass),
                "shm_name": sm.name,
                "run_params": run_params,
            }
            args[-1] = str(config)
            p = Popen(args, stdout=PIPE, stderr=STDOUT, cwd=cwd.as_posix())
            t = ScopeTimer(f"Bake {mesh_pair[0]}[{bake_pass}]", executor.warn)
            executor.update_node_process(i / len(bake_queue))
            while p.poll() is None:
                line = p.stdout.readline().decode("utf-8").strip()
                if not line:
                    continue
                if line.startswith(("Read blend: ", "Info: ", "Blender quit",)):
                    continue
                # 运行时数据 "[RUN_PARAMS]: {"elementsCount": 3}"
                match = re.match(r"\[RUN_PARAMS\]: (\{.*?\})", line, re.S)
                if match:
                    run_params = json.loads(match.group(1))
                    continue
                match = re.match(r"Blender (\d+\.\d+\.\d+)", line)
                if match:
                    continue
                line = line.replace("| Scene ", "")
                match = re.match(r"Fra:(\d+) Mem:(\d+\.\d+)M \(Peak (\d+\.\d+)M\) \| Time:(\d+:.*?) \| Mem:(\d+\.\d+)M, Peak:(\d+\.\d+)M (.*?)xxxx", line + "xxxx", re.S)
                if match:
                    executor.info(f"Fra:{match.group(1)} Mem:{match.group(2)}M {match.group(7)}")
                    continue
                executor.critical(line)
            dst = mesh_pair[0]
            img_name = f"{dst}_{cat}_{bake_pass}"
            if img_name not in bpy.data.images:
                img = bpy.data.images.new(name=img_name, width=res[0], height=res[1], alpha=True)
                img_name = img.name # avoid name changed
            img: bpy.types.Image = bpy.data.images[img_name]
            img.scale(*res)
            img.pixels.foreach_set(pixels.ravel())
            pixels.fill(0)
            bake_result = out_images.ensure_dict(mesh_pair)
            bake_result[(cat, bake_pass)] = img.name

        SHM.erase(sm.name)
        return ctx
        # --tree "Bake Recipe" --node "Output Image Path" --sock -1 --debug 0 --ignorevis 0 --solitr 0 --frameitr 0 --batchitr 0 --rend_dev METAL

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        if self.inputs["Pass"].is_linked:
            return ctx
        bake_passes = ctx.ensure_dict("Pass")
        if self.bake_pass not in bake_passes.get("Internal", []):
            bake_passes.ensure_list("Internal").append(self.bake_pass)
        return ctx


class BakeSetting(NodeBase):
    category = "Core"
    bl_label = "BakeSetting"
    bl_icon = "NODETREE"

    resolution: bpy.props.IntVectorProperty(name="Resolution", size=2, default=(512, 512), min=32, max=16384)
    bake_samples: bpy.props.IntProperty(name="Bake Samples", default=1, min=1, max=4096)
    samples: bpy.props.IntProperty(name="Samples", default=1, min=1, max=4096)
    use_adaptive_sampling: bpy.props.BoolProperty(name="Use Adaptive Sampling", default=True)
    adaptive_threshold: bpy.props.FloatProperty(name="Adaptive Threshold", default=0.0, min=0.0, max=1.0)
    uv_layer: bpy.props.IntProperty(name="UV Layer", default=0, min=0, max=100)

    def init(self, context: Context):
        from ..xxx.preference import get_pref
        self.width = 250
        self.outputs.new("BakeSetting", "Bake Setting")
        self.resolution = get_pref().default_bake_resolution

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        ctx["BakeSettings"] = {
            "resolution": self.resolution[:],
            "bake_samples": self.bake_samples,
            "samples": self.samples,
            "use_adaptive_sampling": self.use_adaptive_sampling,
            "adaptive_threshold": self.adaptive_threshold,
            "uv_layer": self.uv_layer,
        }
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        row = layout.row(align=True)
        row.prop(self, "resolution")
        row.popover(
            panel="OBJECT_PT_bake_setting_presets",
            icon="PRESET",
            text="",
        )
        layout.prop(self, "bake_samples")
        layout.prop(self, "samples")
        row = layout.row(align=True)
        row.prop(self, "use_adaptive_sampling", toggle=True)
        rc = row.column()
        rc.prop(self, "adaptive_threshold", text="")
        rc.enabled = self.use_adaptive_sampling
        layout.prop(self, "uv_layer")


class Pass(NodeBase):
    category = "Pass"
    bl_label = "BlenderPass"
    bl_icon = "NODETREE"

    bake_passes: bpy.props.EnumProperty(items=[("COMBINED", "Combined", "", "NONE", 2 ** 0),
                                               ("AO", "Ambient Occlusion", "", "NONE", 2 ** 1),
                                               ("SHADOW", "Shadow", "", "NONE", 2 ** 2),
                                               ("POSITION", "Position", "", "NONE", 2 ** 3),
                                               ("NORMAL", "Normal", "", "NONE", 2 ** 4),
                                               ("UV", "UV", "", "NONE", 2 ** 5),
                                               ("ROUGHNESS", "Roughness", "", "NONE", 2 ** 6),
                                               ("EMIT", "Emit", "", "NONE", 2 ** 7),
                                               ("ENVIRONMENT", "Environment", "", "NONE", 2 ** 8),
                                               ("DIFFUSE", "Diffuse", "", "NONE", 2 ** 9),
                                               ("GLOSSY", "Glossy", "", "NONE", 2 ** 10),
                                               ("TRANSMISSION", "Transmission", "", "NONE", 2 ** 11),
                                               ],
                                        default={"COMBINED"},
                                        name="Pass",
                                        options={"ENUM_FLAG"})

    def init(self, context: Context):
        self.width = 250
        self.outputs.new("Pass", "Pass")

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        bake_passes = ctx.ensure_dict("Pass")
        for bake_pass in self.bake_passes:
            if bake_pass in bake_passes.get("Internal", []):
                continue
            bake_passes.ensure_list("Internal").append(bake_pass)
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.column().prop(self, "bake_passes")

    def draw_buttons_ext(self, context: Context, layout: UILayout):
        cat = self.bl_label.replace("Pass", "")
        for c in self.bake_passes:
            icon = self.get_icon(cat, c)
            if not icon:
                continue
            box = layout.box()
            box.label(text=c)
            self.draw_icon(box, icon)


class PBRPass(NodeBase):
    category = "Pass"
    bl_label = "PBRPass"
    bl_icon = "NODETREE"

    bake_passes: bpy.props.EnumProperty(items=[("Albedo", "Albedo", "", "NONE", 2 ** 0),
                                               ("Metallic", "Metallic", "", "NONE", 2 ** 1),
                                               ("Roughness", "Roughness", "", "NONE", 2 ** 2),
                                               ("Normal", "Normal", "", "NONE", 2 ** 3),
                                               ("Emission", "Emission", "", "NONE", 2 ** 4),
                                               ("AO", "AO", "", "NONE", 2 ** 5),
                                               ("IOR", "IOR", "", "NONE", 2 ** 6),
                                               ],
                                        default={"Albedo"},
                                        name="Pass",
                                        options={"ENUM_FLAG"})

    def init(self, context: Context):
        self.width = 250
        self.outputs.new("Pass", "Pass")

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        bake_passes = ctx.ensure_dict("Pass")
        for bake_pass in self.bake_passes:
            if bake_pass in bake_passes.get("PBR", []):
                continue
            bake_passes.ensure_list("PBR").append(bake_pass)
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.column().prop(self, "bake_passes")

    def draw_buttons_ext(self, context: Context, layout: UILayout):
        cat = self.bl_label.replace("Pass", "")
        for c in self.bake_passes:
            icon = self.get_icon(cat, c)
            if not icon:
                continue
            box = layout.box()
            box.label(text=c)
            self.draw_icon(box, icon)


class CustomPass(NodeBase):
    __annotations__ = {}
    category = "Pass"
    bl_label = "CustomPass"
    bl_icon = "NODETREE"
    cached_bake_passes = []
    cached_bake_params_name = {}

    def get_bake_passes(self, context):
        if CustomPass.cached_bake_passes:
            return CustomPass.cached_bake_passes
        bake_passes = []
        path = Path(__file__).parent.joinpath("advanced")
        for file in path.glob("*.json"):
            config = json.loads(file.read_text(encoding="utf-8"))
            bake_passes.append((file.stem,
                               config.get("Name"),
                               config.get("Description"),
                               "NONE",
                                2 ** len(bake_passes)))
        CustomPass.cached_bake_passes.extend(bake_passes)
        return CustomPass.cached_bake_passes

    @classmethod
    def gen_prop(cls, ptype, pcfg):
        comm_cfg = {
            "name": pcfg.get("display_name", pcfg["name"]),
            "default": pcfg.get("default"),
        }
        if ptype in {"FLOAT", "VALUE"}:
            comm_cfg["min"] = pcfg.get("min", 0)
            comm_cfg["max"] = pcfg.get("max", 1)
            comm_cfg["step"] = pcfg.get("step", 1)
            return bpy.props.FloatProperty(**comm_cfg)
        elif ptype == "INT":
            comm_cfg["min"] = pcfg.get("min", 0)
            comm_cfg["max"] = pcfg.get("max", 1)
            comm_cfg["step"] = pcfg.get("step", 1)
            return bpy.props.IntProperty(**comm_cfg)
        elif ptype == "VECTOR":
            return bpy.props.FloatVectorProperty(**comm_cfg)
        elif ptype == "RGBA":
            comm_cfg["subtype"] = "COLOR"
            comm_cfg["size"] = 4
            return bpy.props.FloatVectorProperty(**comm_cfg)
        elif ptype == "BOOLEAN":
            return bpy.props.BoolProperty(**comm_cfg)

    @classmethod
    def parse_params(cls):
        dyn_props = {}
        cls.cached_bake_params_name.clear()
        path = Path(__file__).parent.joinpath("advanced")
        for file in path.glob("*.json"):
            config = json.loads(file.read_text(encoding="utf-8"))
            params = config.get("Params", {})
            name_map = {}
            cls.cached_bake_params_name[file.stem] = name_map
            for oname, pcfg in params.items():
                pname = f"{file.stem}_{pcfg['node']}_{pcfg['name']}"
                pname = bpy.path.clean_name(pname)
                dyn_props[pname] = cls.gen_prop(pcfg["type"], pcfg)
                name_map[pname] = oname
        return dyn_props

    @classmethod
    def reg(cls):
        ant = cls.__annotations__
        ant.clear()
        ant["bake_passes"] = bpy.props.EnumProperty(items=cls.get_bake_passes,
                                                    name="Pass",
                                                    options={"ENUM_FLAG"})
        ant.update(cls.parse_params())
        bpy.utils.register_class(cls)

    @classmethod
    def unreg(cls):
        bpy.utils.unregister_class(cls)

    @classmethod
    def reload_node(cls):
        cls.cached_bake_passes.clear()
        cls.unreg()
        cls.reg()

    def init(self, context: Context):
        self.width = 250
        self.outputs.new("Pass", "Pass")

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        bake_passes = ctx.ensure_dict("Pass")
        for bake_pass in self.bake_passes:
            if bake_pass in bake_passes.get("Advanced", []):
                continue
            bake_passes.ensure_list("Advanced").append(bake_pass)
            bake_params_set = ctx.ensure_dict("AdvancedBakeParams")
            bake_params = bake_params_set.ensure_dict(bake_pass)
            name_map = self.cached_bake_params_name[bake_pass]
            for pname in self.__annotations__:
                if pname == "bake_passes":
                    continue
                if not pname.startswith(bake_pass + "_"):
                    continue
                v = getattr(self, pname)
                if isinstance(v, Iterable):
                    v = list(v)
                bake_params[name_map[pname]] = v
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.column().prop(self, "bake_passes")

    def draw_buttons_ext(self, context: Context, layout: UILayout):
        cat = self.bl_label.replace("Pass", "")
        for c in filter(lambda x: x[0] in self.bake_passes, self.cached_bake_passes):
            c = c[0]
            box = layout.box()
            header = box.row()
            header.label(text=c)
            header.operator("bake_tree.delete_bake_preset", text="", icon="TRASH").preset_to_delete = c
            self.draw_icon(box, self.get_icon(cat, c))
            dprops = []
            for pname in self.__annotations__:
                if not pname.startswith(c + "_"):
                    continue
                dprops.append(pname)
            if not dprops:
                continue
            self.draw_export_props(box, dprops)

    def draw_export_props(self, layout: UILayout, dprops: list[str]):
        row = layout.column(align=True)
        for pname in dprops:
            row.prop(self, pname)


class SingleMeshConfig(bpy.types.PropertyGroup):
    enable_uv: bpy.props.BoolProperty(name="Enable UV", default=False)

    def search_uv(self, context, et):
        obj = self.target
        if not obj:
            return []
        return [uv.name for uv in obj.data.uv_layers]

    def update_uv(self, context):
        if not self.uv:
            return
        if not self.target:
            return
        if self.uv not in self.target.data.uv_layers:
            self.uv = ""

    uv: bpy.props.StringProperty(name="UV", search=search_uv, update=update_uv, **common_kwargs)

    def obj_poll(self, obj):
        return obj.type == "MESH"

    target: bpy.props.PointerProperty(type=bpy.types.Object, poll=obj_poll, **common_kwargs)

    def dump(self):
        mesh_pair = ["", "", ""]
        if not self.target:
            return mesh_pair
        mesh_pair[0] = self.target.name
        mesh_pair[2] = self.uv
        return mesh_pair


class SingleMesh(NodeBase):
    category = "Input"
    bl_label = "SingleMesh"
    bl_icon = "NODETREE"

    mesh_configs: bpy.props.CollectionProperty(type=SingleMeshConfig, **common_kwargs)

    def update_add_config(self, context):
        if not self.add_config:
            return
        self.mesh_configs.add()
        self.add_config = False

    add_config: bpy.props.BoolProperty(name="Add Config", default=False, update=update_add_config)

    def init(self, context: Context):
        self.width = 250
        self.outputs.new("Mesh", "Mesh")
        self.mesh_configs.add()

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.prop(self, "add_config", text="", icon="ADD")
        for mesh_config in self.mesh_configs:
            row = layout.row(align=True)
            row.prop(mesh_config, "target", text="")
            if mesh_config.enable_uv:
                row.prop(mesh_config, "uv", text="", icon="UV")
            row.prop(mesh_config, "enable_uv", text="", icon="UV_DATA")

    def dump(self, ctx: TreeCtx = None):
        super().dump(ctx)
        meshes = ctx.ensure_list("Meshes")
        for mesh_config in self.mesh_configs:
            mesh_pair = mesh_config.dump()
            if not mesh_pair[0]:
                continue
            meshes.append(mesh_pair)
        return ctx


class MeshConfig(bpy.types.PropertyGroup):
    enable_uv: bpy.props.BoolProperty(name="Enable UV", default=False)

    def search_uv(self, context, et):
        obj = self.target
        if not obj:
            return []
        return [uv.name for uv in obj.data.uv_layers]

    def update_uv(self, context):
        if not self.uv:
            return
        if not self.target:
            return
        if self.uv not in self.target.data.uv_layers:
            self.uv = ""

    uv: bpy.props.StringProperty(name="UV", search=search_uv, update=update_uv, **common_kwargs)

    def obj_poll(self, obj):
        return obj.type == "MESH"

    target: bpy.props.PointerProperty(type=bpy.types.Object, poll=obj_poll, **common_kwargs)

    source: bpy.props.PointerProperty(type=bpy.types.Object, poll=obj_poll, **common_kwargs)

    def dump(self):
        mesh_pair = ["", "", ""]
        if not self.target:
            return mesh_pair
        mesh_pair[0] = self.target.name
        if self.source:
            mesh_pair[1] = self.source.name
        mesh_pair[2] = self.uv
        return mesh_pair


class Mesh(NodeBase):
    category = "Input"
    bl_label = "Mesh"
    bl_icon = "NODETREE"

    mesh_configs: bpy.props.CollectionProperty(type=MeshConfig, **common_kwargs)

    def update_add_config(self, context):
        if not self.add_config:
            return
        self.mesh_configs.add()
        self.add_config = False

    add_config: bpy.props.BoolProperty(name="Add Config", default=False, update=update_add_config)

    def init(self, context: Context):
        self.width = 400
        self.outputs.new("Mesh", "Mesh")

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.prop(self, "add_config", text="", icon="ADD")
        for mesh_config in self.mesh_configs:
            row = layout.row(align=True)
            row.prop(mesh_config, "target")
            if mesh_config.enable_uv:
                row.prop(mesh_config, "uv", text="", icon="UV")
            row.prop(mesh_config, "enable_uv", text="", icon="UV_DATA")
            row.prop(mesh_config, "source")

    def dump(self, ctx: TreeCtx = None):
        super().dump(ctx)
        meshes = ctx.ensure_list("Meshes")
        for mesh_config in self.mesh_configs:
            mesh_pair = mesh_config.dump()
            if not mesh_pair[0]:
                continue
            meshes.append(mesh_pair)
        return ctx


class SceneMeshes(NodeBase):
    category = "Input"
    bl_label = "SceneMeshes"
    bl_icon = "NODETREE"

    scene: bpy.props.PointerProperty(type=bpy.types.Scene)

    def init(self, context: Context):
        self.width = 250
        self.outputs.new("Mesh", "Mesh")

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.prop(self, "scene")

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        meshes = ctx.ensure_list("Meshes")
        meshes += [[o.name, "", ""] for o in self.scene.objects if o.type == "MESH"]
        return ctx


class CollectionMesh(NodeBase):
    category = "Input"
    bl_label = "CollectionMeshes"
    bl_icon = "NODETREE"

    collection: bpy.props.PointerProperty(type=bpy.types.Collection)

    def init(self, context: Context):
        self.outputs.new("Mesh", "Mesh")

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.prop(self, "collection")

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        meshes = ctx.ensure_list("Meshes")
        meshes += [[o.name, "", ""] for o in self.collection.objects if o.type == "MESH"]
        return ctx


class ImageCombine(NodeBase):
    category = "Other"
    bl_label = "ImageCombine"
    bl_icon = "NODETREE"

    combine: bpy.props.BoolProperty(name="Combine", default=True)
    resize: bpy.props.BoolProperty(name="Resize", default=False)
    output_resolution: bpy.props.IntVectorProperty(name="Resolution", size=2, default=(512, 512), min=32)

    def init(self, context: Context):
        self.width = 250
        self.inputs.new("Image", "Image")
        self.outputs.new("Image", "Image")

    def draw_buttons(self, context: Context, layout: UILayout):
        layout.prop(self, "combine", toggle=True)
        layout.prop(self, "resize", toggle=True)
        if self.resize:
            layout.prop(self, "output_resolution")

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        res = super().execute(executor, task, *args, **kwargs)
        image_combine = task.get("ImageCombine", {})
        if not image_combine.get("combine", False):
            return res
        out_images: dict = task.get("OutImages", {})

        bake_images = {}
        for pair, images in out_images.items():
            for (cat, bake_pass), _name in images.items():
                img = bpy.data.images.get(_name)
                if not img:
                    continue
                bake_images.setdefault((cat, bake_pass), []).append(img)

        bake_settings = task.get("BakeSettings", {})
        reslution = bake_settings.get("resolution", (512, 512))
        for (cat, bake_pass), images in bake_images.items():
            if len(images) < 2:
                continue
            background = np.zeros(4, dtype=np.float32)
            canvas = np.zeros((reslution[0], reslution[1], 4), dtype=np.float32)
            background[3] = 1
            if bake_pass.endswith("Normal"):
                background[:] = (0.501960813999176, 0.501960813999176, 1, 1)
            canvas[:, :] = background
            for img in images:
                pixels = np.zeros(reslution[0] * reslution[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(pixels)
                pixels = pixels.reshape(reslution[0], reslution[1], 4)
                has_data = np.abs(pixels[:, :, :3] - background[:3]).sum(axis=2) > 0.000001
                # 所有物体的同烘焙类型的图片按alpha上叠算法合并
                canvas[has_data] = pixels[has_data]
            combine_img = bpy.data.images.new(name=f"{cat}_{bake_pass}_Combine",
                                              width=reslution[0],
                                              height=reslution[1],
                                              alpha=True)
            combine_img.pixels.foreach_set(canvas.ravel())
            if "resize" not in image_combine:
                continue
            combine_img.scale(*image_combine["output_resolution"])
        return res

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        if not self.combine:
            return ctx
        if not self.inputs["Image"].is_linked:
            return ctx
        image_combine = ctx.ensure_dict("ImageCombine")
        image_combine["combine"] = self.combine
        image_combine["resize"] = self.resize
        image_combine["output_resolution"] = self.output_resolution[:]


class AITexPreview(NodeBase):
    __annotations__ = {}
    category = "Output"
    bl_label = "AITexPreview"
    bl_icon = "NODETREE"

    def init(self, context: Context):
        self.is_output = True
        self.width = 220
        self.inputs.new("Image", "Image")

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        res = super().execute(executor, task, *args, **kwargs)
        out_images: dict = task.get("OutImages", {})

        for pair, images in out_images.items():
            for (cat, bake_pass), _name in images.items():
                img = bpy.data.images.get(_name)
                if not img:
                    continue
                # 新建预览材质
                # 新建几何节点, 功能为: 将以上材质添加为当前物体默认材质
                # 新建几何节点修改器

                obj = bpy.data.objects.get(pair[0])
                if not obj:
                    continue
                ori: bpy.types.Object = obj.get("AI_Mat_Gen_Ori")
                if not ori:
                    return {"CANCELLED"}
                mat = bpy.data.materials.new(name=f"AI_Tex_Preview_{ori.name}")
                mat.use_nodes = True
                tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
                tex.location.x = -300
                tex.image = img
                mat.node_tree.links.new(mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"], tex.outputs[0])
                node_group = bpy.data.node_groups.new(name=f"AI_Tex_Preview_GN_{ori.name}", type="GeometryNodeTree")
                _input = node_group.nodes.new("NodeGroupInput")
                _input.location.x = -160
                _output = node_group.nodes.new("NodeGroupOutput")
                _output.location.x = 160
                _set_mat = node_group.nodes.new("GeometryNodeSetMaterial")
                _set_mat.inputs["Material"].default_value = mat
                node_group.interface.new_socket("GN", in_out="INPUT", socket_type="NodeSocketGeometry")
                node_group.interface.new_socket("GN", in_out="OUTPUT", socket_type="NodeSocketGeometry")
                node_group.interface_update(bpy.context)
                node_group.links.new(_set_mat.inputs[0], _input.outputs[0])
                node_group.links.new(_output.inputs[0], _set_mat.outputs[0])
                mod = ori.modifiers.new(name=f"AI_Tex_Preview_Mod_{ori.name}", type="NODES")
                mod.node_group = node_group
                break
        return res

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        return ctx


class SaveToImage(NodeBase):
    __annotations__ = {}
    category = "Output"
    bl_label = "SaveToImage"
    bl_icon = "NODETREE"
    img_data_block = {
        "BMP": ["color_mode"],
        "IRIS": ["color_mode"],
        "PNG": ["color_mode", "color_depth", "compression"],
        "JPEG": ["color_mode", "quality"],
        "JPEG2000": ["color_mode", "color_depth", "quality", "jpeg2k_codec", "use_jpeg2k_cinema_preset", "use_jpeg2k_cinema_48", "use_jpeg2k_ycc"],
        "TARGA": ["color_mode"],
        "TARGA_RAW": ["color_mode"],
        "CINEON": ["color_mode"],
        "DPX": ["color_mode", "color_depth", "use_cineon_log"],
        "OPEN_EXR_MULTILAYER": ["color_depth", "exr_codec", "use_preview"],
        "OPEN_EXR": ["color_mode", "color_depth", "exr_codec", "use_preview"],
        "HDR": ["color_mode"],
        "TIFF": ["color_mode", "color_depth", "tiff_codec"],
        "WEBP": ["color_mode", "quality"],
    }

    def init(self, context: Context):
        self.is_output = True
        self.width = 220
        self.inputs.new("Image", "Image")

    def update_directory(self, context: Context):
        p = Path(bpy.path.abspath(self.directory)).resolve().absolute().as_posix()
        if p == self.directory:
            return
        self["directory"] = p
    directory: bpy.props.StringProperty(subtype="DIR_PATH", update=update_directory)

    for p in bpy.types.ImageFormatSettings.bl_rna.properties:
        prop = None
        if p.type == "POINTER":
            continue
        kwargs = {
            "name": p.name,
            "description": p.description,
            "subtype": p.subtype,
            "default": p.default,
        }
        if bpy.app.version >= (4, 0, 0):
            kwargs["translation_context"] = p.translation_context
        match p.type:
            case "ENUM":
                kwargs.pop("subtype")
                kwargs["items"] = [(ei.identifier, ei.name, ei.description, ei.icon, ei.value) for ei in p.enum_items]
                prop = bpy.props.EnumProperty(**kwargs)
            case "BOOLEAN":
                prop = bpy.props.BoolProperty(**kwargs)
            case "INT":
                kwargs["soft_min"] = p.soft_min
                kwargs["soft_max"] = p.soft_max
                kwargs["step"] = p.step
                prop = bpy.props.IntProperty(**kwargs)
            case "FLOAT":
                kwargs["soft_min"] = p.soft_min
                kwargs["soft_max"] = p.soft_max
                kwargs["step"] = p.step
                prop = bpy.props.FloatProperty(**kwargs)
            case "STRING":
                prop = bpy.props.StringProperty(**kwargs)

        if prop:
            __annotations__[p.identifier] = prop

    seperator: bpy.props.StringProperty(default="_", name="Seperator", **common_kwargs)

    name_fmt: bpy.props.StringProperty(default="{obj_name}_{bake_pass}", **common_kwargs)

    NAME_FMT_ITEMS = [
        "obj_name",
        "bake_cat",
        "bake_pass",
        "resolution",
    ]
    for fmt in NAME_FMT_ITEMS:
        def get_update_fmt(fmt):
            def update_fmt(self, context):
                if not getattr(self, f"set_fmt_{fmt}"):
                    return
                setattr(self, f"set_fmt_{fmt}", False)
                fmts = re.findall(r"({[a-zA-Z_]+})", self.name_fmt)
                append_fmt = f"{{{fmt}}}"
                if not fmts:
                    self.name_fmt = append_fmt
                    return
                if append_fmt in fmts:
                    fmts.remove(append_fmt)
                else:
                    fmts.append(append_fmt)
                self.name_fmt = self.seperator.join(fmts)
            return update_fmt
        prop = bpy.props.BoolProperty(name=fmt,
                                      default=False,
                                      update=get_update_fmt(fmt),
                                      **common_kwargs,
                                      )
        __annotations__[f"set_fmt_{fmt}"] = prop

    def color_mode_items(self, context):
        if self.file_format in {"BMP", "JPEG", "CINEON", "HDR"}:
            return [
                ("BW", "BW", "", "NONE", 0),
                ("RGB", "RGB", "", "NONE", 1),
            ]
        return [
            ("BW", "BW", "", "NONE", 0),
            ("RGB", "RGB", "", "NONE", 1),
            ("RGBA", "RGBA", "", "NONE", 2),
        ]

    color_mode: bpy.props.EnumProperty(items=color_mode_items, name="Color Mode", default=1)

    def color_depth_items(self, context):
        m0 = [
            ("8", "8", "", "NONE", 0),
            ("16", "16", "", "NONE", 1),
        ]
        m1 = [
            ("8", "8", "", "NONE", 0),
            ("12", "12", "", "NONE", 1),
            ("16", "16", "", "NONE", 2),
        ]
        m2 = [
            ("8", "8", "", "NONE", 0),
            ("10", "10", "", "NONE", 1),
            ("12", "12", "", "NONE", 2),
            ("16", "16", "", "NONE", 3),
        ]
        m3 = [
            ("16", "Float (Half)", "", "NONE", 0),
            ("32", "Float (Full)", "", "NONE", 1),
        ]
        if self.file_format in {"PNG", "TIFF"}:
            return m0
        if self.file_format in {"JPEG2000", }:
            return m1
        if self.file_format in {"DPX", }:
            return m2
        if self.file_format in {"OPEN_EXR_MULTILAYER", "OPEN_EXR"}:
            return m3
        return m0

    color_depth: bpy.props.EnumProperty(items=color_depth_items, name="Color Depth")

    def exr_codec_items(self, context):
        ec = [
            ('NONE', 'None', '', 'NONE', 0),
            ('PXR24', 'Pxr24 (lossy)', '', 'NONE', 1),
            ('ZIP', 'ZIP (lossless)', '', 'NONE', 2),
            ('PIZ', 'PIZ (lossless)', '', 'NONE', 3),
            ('RLE', 'RLE (lossless)', '', 'NONE', 4),
            ('ZIPS', 'ZIPS (lossless)', '', 'NONE', 5),
            ('B44', 'B44 (lossy)', '', 'NONE', 6),
            ('B44A', 'B44A (lossy)', '', 'NONE', 7),
            ('DWAA', 'DWAA (lossy)', '', 'NONE', 8),
            ('DWAB', 'DWAB (lossy)', '', 'NONE', 9)
        ]
        if self.color_depth == "32":
            ec.pop(6)
            ec.pop(6)
        return ec

    exr_codec: bpy.props.EnumProperty(items=exr_codec_items, name="EXR Codec")

    @staticmethod
    def calc_name_fmt(fmt, sep):
        if not fmt:
            fmt = "{obj_name}_{bake_pass}"
        return fmt.replace("}_{", f"}}{sep}{{")

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        res = super().execute(executor, task, *args, **kwargs)
        config = task.get("SaveConfig", {})
        out_images: dict = task.get("OutImages", {})
        directory = config.get("Directory", "")
        img_settings = config.ensure_dict("ImageSettings")
        name_fmt = config.get("NameFormat", "")
        seperator = config.get("Seperator", "_")
        name_fmt = cls.calc_name_fmt(name_fmt, seperator)
        scene = bpy.context.scene
        render = scene.render
        old_img_settings = {}

        for key in img_settings:
            old_img_settings[key] = getattr(render.image_settings, key)

        @Timer.wait_run
        def f():
            for key in img_settings:
                setattr(render.image_settings, key, img_settings[key])

        f()
        img_suffix = "." + img_settings["file_format"].lower()
        for pair, images in out_images.items():
            for (cat, bake_pass), _name in images.items():
                img = bpy.data.images.get(_name)
                if not img:
                    continue
                img_name = name_fmt.format(obj_name=pair[0],
                                           bake_cat=cat,
                                           bake_pass=bake_pass,
                                           resolution=f"{img.size[0]}x{img.size[1]}",
                                           )
                t = ScopeTimer(f"Save {img_name}", executor.warn)
                img_path = Path(directory) / (img_name + img_suffix)
                img_path.unlink(missing_ok=True)
                img.save_render(filepath=img_path.as_posix(), scene=scene)
                # bpy.data.images.remove(img)

        @Timer.wait_run
        def f():
            for key in old_img_settings:
                setattr(render.image_settings, key, old_img_settings[key])

        f()
        return res

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        config = ctx.ensure_dict("SaveConfig")
        config["Directory"] = self.directory
        config["Seperator"] = self.seperator
        config["NameFormat"] = self.name_fmt
        img_settings = config.ensure_dict("ImageSettings")
        img_settings["file_format"] = self.file_format

        disp_props = self.img_data_block.get(self.file_format, [])
        for p in disp_props:
            img_settings[p] = getattr(self, p)
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        row = layout.row(align=True)
        row.label(text="Name Format:", text_ctxt=NODE_TCTX)
        fmts = re.findall(r"\{([a-zA-Z_]+)\}", self.name_fmt)
        for fmt in self.NAME_FMT_ITEMS:
            row.alert = fmt in fmts
            row.prop(self, f"set_fmt_{fmt}", toggle=True)
        layout.prop(self, "seperator")
        name_fmt_example = {
            "obj_name": "Cube",
            "bake_cat": "PBR",
            "bake_pass": "Diffuse",
            "resolution": "64x64",
        }

        fmt = self.calc_name_fmt(self.name_fmt, self.seperator)
        if fmt:
            row = layout.row()
            row.alignment = "LEFT"
            row.label(text="Name Example:", text_ctxt=NODE_TCTX)
            row.label(text=fmt.format(**name_fmt_example), translate=False)

        layout.prop(self, "directory")
        layout.prop(self, "file_format")

        if not (disp_props := self.img_data_block.get(self.file_format, [])):
            return
        box = layout.box()
        for p in disp_props:
            if p in {"color_mode", "color_depth"}:
                box.row().prop(self, p, expand=True)
            else:
                box.prop(self, p)


class SaveToMat(NodeBase):
    __annotations__ = {}
    category = "Output"
    bl_label = "SaveToMat"
    bl_icon = "NODETREE"
    exclude = True

    def init(self, context: Context):
        self.is_output = True
        self.width = 220
        self.inputs.new("Image", "Image")

    seperator: bpy.props.StringProperty(default="_", name="Seperator", **common_kwargs)

    name_fmt: bpy.props.StringProperty(default="{obj_name}_{bake_pass}", **common_kwargs)

    NAME_FMT_ITEMS = [
        "obj_name",
        "bake_cat",
        "bake_pass",
        "resolution",
    ]
    for fmt in NAME_FMT_ITEMS:
        def get_update_fmt(fmt):
            def update_fmt(self, context):
                if not getattr(self, f"set_fmt_{fmt}"):
                    return
                setattr(self, f"set_fmt_{fmt}", False)
                fmts = re.findall(r"({[a-zA-Z_]+})", self.name_fmt)
                append_fmt = f"{{{fmt}}}"
                if not fmts:
                    self.name_fmt = append_fmt
                    return
                if append_fmt in fmts:
                    fmts.remove(append_fmt)
                else:
                    fmts.append(append_fmt)
                self.name_fmt = self.seperator.join(fmts)
            return update_fmt
        prop = bpy.props.BoolProperty(name=fmt,
                                      default=False,
                                      update=get_update_fmt(fmt),
                                      **common_kwargs,
                                      )
        __annotations__[f"set_fmt_{fmt}"] = prop

    @staticmethod
    def calc_name_fmt(fmt, sep):
        if not fmt:
            fmt = "{obj_name}_{bake_pass}"
        return fmt.replace("}_{", f"}}{sep}{{")

    @classmethod
    def execute(cls, executor: TaskExecutor, task: TreeCtx, *args, **kwargs) -> dict:
        res = super().execute(executor, task, *args, **kwargs)
        config = task.get("SaveConfig", {})
        out_images: dict = task.get("OutImages", {})
        name_fmt = config.get("NameFormat", "")
        seperator = config.get("Seperator", "_")
        name_fmt = cls.calc_name_fmt(name_fmt, seperator)

        for pair, images in out_images.items():
            for (cat, bake_pass), _name in images.items():
                img = bpy.data.images.get(_name)
                if not img:
                    continue
        return res

    def dump(self, ctx: TreeCtx = None) -> TreeCtx:
        super().dump(ctx)
        config = ctx.ensure_dict("SaveConfig")
        config["Seperator"] = self.seperator
        config["NameFormat"] = self.name_fmt
        return ctx

    def draw_buttons(self, context: Context, layout: UILayout):
        row = layout.row(align=True)
        row.label(text="Name Format:", text_ctxt=NODE_TCTX)
        fmts = re.findall(r"\{([a-zA-Z_]+)\}", self.name_fmt)
        for fmt in self.NAME_FMT_ITEMS:
            row.alert = fmt in fmts
            row.prop(self, f"set_fmt_{fmt}", toggle=True)
        layout.prop(self, "seperator")
        name_fmt_example = {
            "obj_name": "Cube",
            "bake_cat": "PBR",
            "bake_pass": "Diffuse",
            "resolution": "64x64",
        }

        fmt = self.calc_name_fmt(self.name_fmt, self.seperator)
        if not fmt:
            return
        row = layout.row()
        row.alignment = "LEFT"
        row.label(text="Name Example:", text_ctxt=NODE_TCTX)
        row.label(text=fmt.format(**name_fmt_example), translate=False)

import bpy
from bpy.types import Context
from .operators import BakeTreeRun, BakeSettingsPresetsOps, PrefBakeSettingsPresetsOps, SaveAsBakePresets, MarkPropAsParams, DeletePropFromParams, BakeTreePresetsOps
from ..node_tree.node_tree import TREE_TYPE
from .preference import get_pref
PROP_CTX = "BakeNodeProp"
PANEL_CTX = "BakeNodePanel"


class AIBakeTree(bpy.types.Panel):
    bl_idname = "SDN_BT_PT_UI"
    bl_translation_context = "SDN"
    bl_label = "Bake Tree"
    bl_description = ""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI"
    bl_order = 99

    def draw(self, context: Context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(get_pref(), "default_bake_resolution")
        row.popover(
            panel=PrefBakeSettingPresets.bl_idname,
            icon="PRESET",
            text="",
        )
        trees = [t for t in bpy.data.node_groups if t.bl_idname == "BakeNodeTree"]
        if not trees:
            layout.label(text="No Bake Tree Found")
            return
        if not hasattr(bpy.context.scene, "sdn"):
            return
        self.show_common(layout)
        self.show_nodes(layout)

    def show_common(self, layout):
        layout = self.layout
        layout.prop(bpy.context.scene.sdn, "bake_tree", text="")

    def show_nodes(self, layout: bpy.types.UILayout):
        nodes = []
        tree_name = bpy.context.scene.sdn.bake_tree
        tree: bpy.types.NodeTree = bpy.data.node_groups.get(tree_name)
        if not tree:
            return
        for node in tree.nodes:
            if len(node.label) != 3:
                continue
            # 判断 label 为 001 - 999 之间的字符串
            if not node.label.isdigit() or int(node.label) < 1 or int(node.label) > 999:
                continue
            nodes.append(node)
        nodes.sort(key=lambda x: x.label)
        for node in nodes:
            box = layout.box()
            row = box.row()
            row.prop(node, "ac_expand", icon="TRIA_DOWN" if node.ac_expand else "TRIA_RIGHT", text="", emboss=False)
            if node.type != "GROUP":
                row.label(text=node.name)
            elif node.node_tree:
                row.label(text=node.node_tree.name)
            if node.ac_expand is False:
                continue
            if bpy.app.version >= (4, 2):
                box.separator(type="LINE")
            else:
                box.separator()
            node.draw_buttons(bpy.context, box)


class BakeTreePanel(bpy.types.Panel):
    bl_idname = "OBJECT_PT_bake_tree"
    bl_label = "Easy Bake Node"
    bl_category = "Easy Bake Node"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context: Context):
        return context.space_data.tree_type == TREE_TYPE

    def draw(self, context):
        layout = self.layout
        self.show_common(layout)
        self.show_preset(layout)

    def show_common(self, layout: bpy.types.UILayout):
        layout.operator(BakeTreeRun.bl_idname)
        wm = bpy.context.window_manager
        props = wm.bake_tree
        col = layout.column()
        col.alert = props.tnum != 0
        col.prop(props, "tnum")
        if props.etask:
            box = layout.box()
            row = box.row()
            row.label(text="Exe Task", text_ctxt=PROP_CTX)
            row.label(text=props.etask)
            box.prop(props, "etask_p")
        if props.enode:
            box = layout.box()
            row = box.row()
            row.label(text="Exe Node", text_ctxt=PROP_CTX)
            row.label(text=props.enode)
            box.prop(props, "enode_p")

        node = bpy.context.active_node
        if not node or not hasattr(node, "draw_buttons_ext"):
            return
        node.draw_buttons_ext(bpy.context, layout)

    def show_preset(self, layout: bpy.types.UILayout):
        layout.prop(bpy.context.window_manager.bake_tree, "preset", text="")
        layout.prop(bpy.context.window_manager.bake_tree, "preset_save_name")
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator(BakeTreePresetsOps.bl_idname, text="Load", icon="EXPORT").action = "LOAD"
        row.operator(BakeTreePresetsOps.bl_idname, text="Save", icon="IMPORT").action = "SAVE"
        row.operator(BakeTreePresetsOps.bl_idname, text="Delete", icon="TRASH").action = "DEL"


class BakeSettingPresets(bpy.types.Panel):
    bl_idname = "OBJECT_PT_bake_setting_presets"
    bl_label = "Bake Settings Presets"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "HEADER"
    bl_translation_context = PANEL_CTX

    def draw(self, context):
        layout = self.layout
        resolutions = {
            "512x512": (512, 512),
            "640x480": (640, 480),
            "1280x720": (1280, 720),
            "1920x1080": (1920, 1080),
            "2560x1440": (2560, 1440),
            "3840x2160": (3840, 2160),
            "7680x4320": (7680, 4320),
            "1k(1024x1024)": (1024, 1024),
            "2k(2048x2048)": (2048, 2048),
            "4k(4096x4096)": (4096, 4096),
            "8k(8192x8192)": (8192, 8192),
            "16k(16384x16384)": (16384, 16384),
        }
        col = layout.column(align=True)
        for name, (w, h) in resolutions.items():
            col.operator(BakeSettingsPresetsOps.bl_idname, text=name).resolution = (w, h)


class PrefBakeSettingPresets(bpy.types.Panel):
    bl_idname = "OBJECT_PT_pref_bake_setting_presets"
    bl_label = "Bake Settings Presets"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "HEADER"
    bl_translation_context = PANEL_CTX

    def draw(self, context):
        layout = self.layout
        resolutions = {
            "512x512": (512, 512),
            "640x480": (640, 480),
            "1280x720": (1280, 720),
            "1920x1080": (1920, 1080),
            "2560x1440": (2560, 1440),
            "3840x2160": (3840, 2160),
            "7680x4320": (7680, 4320),
            "1k(1024x1024)": (1024, 1024),
            "2k(2048x2048)": (2048, 2048),
            "4k(4096x4096)": (4096, 4096),
            "8k(8192x8192)": (8192, 8192),
            "16k(16384x16384)": (16384, 16384),
        }
        col = layout.column(align=True)
        for name, (w, h) in resolutions.items():
            col.operator(PrefBakeSettingsPresetsOps.bl_idname, text=name).resolution = (w, h)


class MatPanel(bpy.types.Panel):
    bl_idname = "OBJECT_PT_bake_mat"
    bl_label = "Bake Tree"
    bl_category = "Bake Tree"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context: Context):
        return context.space_data.tree_type == "ShaderNodeTree"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        tree_prop = context.window_manager.bake_tree
        rcol = row.column()
        rcol.enabled = not tree_prop.mat_name_as_save_name
        rcol.prop(tree_prop, "mat_preset_save_name")
        row.prop(tree_prop, "mat_name_as_save_name", toggle=True, text="", icon="NODE_MATERIAL")
        layout.prop(tree_prop, "mat_preset_description")
        layout.prop(bpy.context.scene.cycles, "bake_pass")
        layout.operator(SaveAsBakePresets.bl_idname)
        mtl = context.material
        if not mtl:
            return
        bake_params = mtl.bake_params
        if not bake_params:
            return
        box = layout.box()
        rbox = box.row()
        rbox.label(text="Bake Params", text_ctxt=PANEL_CTX)
        rbox.operator(DeletePropFromParams.bl_idname, text="", icon="PANEL_CLOSE").action = "ALL"
        for name, prop in bake_params.items():
            prop_attr = prop["data_path"]
            p = mtl
            if "." in prop_attr:
                prop_path, prop_attr = prop_attr.rsplit(".", 1)
                try:
                    p = mtl.path_resolve(prop_path)
                except ValueError:
                    rbox = box.row()
                    rbox.alert = True
                    rbox.prop(prop, "dname", text="Name", icon="ERROR")
                    op = rbox.operator(DeletePropFromParams.bl_idname, text="", icon="PANEL_CLOSE")
                    op.action = "ONE"
                    op.prop_to_del = name
                    continue
            rbox = box.row(align=True)
            rbox.prop(prop, "dname", text="Name")
            rbox.prop(p, prop_attr, text="")
            op = rbox.operator(DeletePropFromParams.bl_idname, text="", icon="PANEL_CLOSE")
            op.action = "ONE"
            op.prop_to_del = name


def draw_node_prop(self, context):
    if context.space_data.type != "NODE_EDITOR":
        return
    if context.space_data.tree_type != "ShaderNodeTree":
        return
    node: bpy.types.Node = getattr(bpy.context, "node", None)
    if not node:
        return
    if not context.property:
        return
    property = context.property
    if property[1] != "default_value":
        try:
            ptype = node.bl_rna.properties[property[1]].type
        except BaseException:
            return
        if ptype not in {"BOOLEAN", "INT", "FLOAT", "VECTOR", "RGBA"}:
            return
    self.layout.operator(MarkPropAsParams.bl_idname)

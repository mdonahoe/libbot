#!/usr/bin/env python

import sys
import time
import traceback
import getopt
import subprocess
import signal
import pickle

import glib
import gobject
import gtk
import pango
import os

from lcm import LCM

from bot_procman.orders_t import orders_t
from bot_procman.printf_t import printf_t
import bot_procman.sheriff as sheriff
import bot_procman.sheriff_config as sheriff_config

try:
    from build_prefix import BUILD_PREFIX
except ImportError:
    BUILD_PREFIX = None

PRINTF_RATE_LIMIT = 10000
UPDATE_CMDS_MIN_INTERVAL_USEC = 300

ANSI_CODES_TO_TEXT_TAG_PROPERTIES = { \
        "1" : ("weight", pango.WEIGHT_BOLD),
        "2" : ("weight", pango.WEIGHT_LIGHT),
        "4" : ("underline", pango.UNDERLINE_SINGLE),
        "30" : ("foreground", "black"),
        "31" : ("foreground", "red"),
        "32" : ("foreground", "green"),
        "33" : ("foreground", "yellow"),
        "34" : ("foreground", "blue"),
        "35" : ("foreground", "magenta"),
        "36" : ("foreground", "cyan"),
        "37" : ("foreground", "white"),
        "40" : ("background", "black"),
        "41" : ("background", "red"),
        "42" : ("background", "green"),
        "43" : ("background", "yellow"),
        "44" : ("background", "blue"),
        "45" : ("background", "magenta"),
        "46" : ("background", "cyan"),
        "47" : ("background", "white"),
        }

COL_CMDS_TV_OBJ, \
COL_CMDS_TV_CMD, \
COL_CMDS_TV_NICKNAME, \
COL_CMDS_TV_HOST, \
COL_CMDS_TV_STATUS_ACTUAL, \
COL_CMDS_TV_CPU_USAGE, \
COL_CMDS_TV_MEM_VSIZE, \
COL_CMDS_TV_AUTO_RESPAWN, \
NUM_CMDS_ROWS = range(9)

COL_HOSTS_TV_OBJ, \
COL_HOSTS_TV_NAME, \
COL_HOSTS_TV_LAST_UPDATE, \
COL_HOSTS_TV_LOAD, \
NUM_HOSTS_ROWS = range(5)

def find_bot_procman_deputy_cmd():
    search_path = []
    if BUILD_PREFIX is not None:
        search_path.append("%s/bin" % BUILD_PREFIX)
    search_path.extend(os.getenv("PATH").split(":"))
    for dirname in search_path:
        fname = "%s/bot-procman-deputy" % dirname
        if os.path.exists(fname) and os.path.isfile(fname):
            return fname
    return None

def timestamp_now (): return int (time.time () * 1000000)

def now_str (): return time.strftime ("[%H:%M:%S] ")

class AddModifyCommandDialog (gtk.Dialog):
    def __init__ (self, parent, deputies, groups,
            initial_cmd="", initial_nickname="", initial_deputy=None, 
            initial_group="", initial_auto_respawn=False):
        # add command dialog
        gtk.Dialog.__init__ (self, "Add/Modify Command", parent,
                gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                (gtk.STOCK_OK, gtk.RESPONSE_ACCEPT,
                 gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT))
        table = gtk.Table (4, 2)

        # deputy
        table.attach (gtk.Label ("Host"), 0, 1, 0, 1, 0, 0)
        self.deputy_ls = gtk.ListStore (gobject.TYPE_PYOBJECT, 
                gobject.TYPE_STRING)
        self.host_cb = gtk.ComboBox (self.deputy_ls)

        dep_ind = 0
        pairs = [ (deputy.name, deputy) for deputy in deputies ]
        pairs.sort ()
        for name, deputy in pairs:
            self.deputy_ls.append ((deputy, deputy.name))
            if deputy == initial_deputy: 
                self.host_cb.set_active (dep_ind)
            dep_ind += 1
        if self.host_cb.get_active () < 0 and len(deputies) > 0:
            self.host_cb.set_active (0)

        deputy_tr = gtk.CellRendererText ()
        self.host_cb.pack_start (deputy_tr, True)
        self.host_cb.add_attribute (deputy_tr, "text", 1)
        table.attach (self.host_cb, 1, 2, 0, 1)
        self.deputies = deputies

        # command name
        table.attach (gtk.Label ("Command"), 0, 1, 1, 2, 0, 0)
        self.name_te = gtk.Entry ()
        self.name_te.set_text (initial_cmd)
        self.name_te.set_width_chars (60)
        table.attach (self.name_te, 1, 2, 1, 2)
        self.name_te.connect ("activate", 
                lambda e: self.response (gtk.RESPONSE_ACCEPT))
        self.name_te.grab_focus ()

        # command nickname
        table.attach (gtk.Label ("Nickname"), 0, 1, 2, 3, 0, 0)
        self.nickname_te = gtk.Entry ()
        self.nickname_te.set_text (initial_nickname)
        self.nickname_te.set_width_chars (60)
        table.attach (self.nickname_te, 1, 2, 2, 3)
        self.nickname_te.connect ("activate", 
                lambda e: self.response (gtk.RESPONSE_ACCEPT))

        # group
        table.attach (gtk.Label ("Group"), 0, 1, 3, 4, 0, 0)
        self.group_cbe = gtk.combo_box_entry_new_text ()
#        groups = groups[:]
        groups.sort ()
        for group_name in groups:
            self.group_cbe.append_text (group_name)
        table.attach (self.group_cbe, 1, 2, 3, 4)
        self.group_cbe.child.set_text (initial_group)
        self.group_cbe.child.connect ("activate",
                lambda e: self.response (gtk.RESPONSE_ACCEPT))

        # auto respawn
        table.attach (gtk.Label ("Auto-restart"), 0, 1, 4, 5, 0, 0)
        self.auto_respawn_cb = gtk.CheckButton ()
        self.auto_respawn_cb.set_active (initial_auto_respawn)
        table.attach (self.auto_respawn_cb, 1, 2, 4, 5)

        self.vbox.pack_start (table, False, False, 0)
        table.show_all ()

    def get_deputy (self):
        host_iter = self.host_cb.get_active_iter ()
        if host_iter is None: return None
        return self.deputy_ls.get_value (host_iter, 0)
    
    def get_command (self): return self.name_te.get_text ()
    def get_nickname (self): return self.nickname_te.get_text ()
    def get_group (self): return self.group_cbe.child.get_text ()
    def get_auto_respawn (self): return self.auto_respawn_cb.get_active ()

class CommandExtraData(object):
    def __init__ (self, text_tag_table):
        self.tb = gtk.TextBuffer (text_tag_table)
        self.printf_keep_count = [ 0, 0, 0, 0, 0, 0 ]
        self.printf_drop_count = 0

class SheriffGtkConfig(object):
    def __init__(self):
        self.show_columns = [ True ] * NUM_CMDS_ROWS
        config_dir = os.path.join(glib.get_user_config_dir(), "procman-sheriff")
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        self.config_fname = os.path.join(config_dir, "config")
    
    def save(self):
        d = {}
        for i, val in enumerate(self.show_columns):
            d["show_column_%d" % i] = val

        try:
            pickle.dump(d, open(self.config_fname, "w"))
        except Exception, err:
            print err

    def load(self):
        if not os.path.exists(self.config_fname):
            return
        try:
            d = pickle.load(open(self.config_fname, "r"))
            for i in range(len(self.show_columns)):
                self.show_columns[i] = d["show_column_%d" % i]
        except Exception, err:
            print err
            return

class SheriffGtk(object):
    def __init__ (self, lc):
        self.lc = lc
        self.stdout_maxlines = 2000
        self.config_filename = None
        self.next_cmds_update_time = 0
        self.gui_config = SheriffGtkConfig()
        self.gui_config.load()

        # deputy spawned by the sheriff
        self.spawned_deputy = None

        # create sheriff and subscribe to events
        self.sheriff = sheriff.Sheriff (self.lc)
        self.sheriff.connect ("command-added", self._on_sheriff_command_added)
        self.sheriff.connect ("command-removed", 
                self._on_sheriff_command_removed)
        self.sheriff.connect ("command-status-changed",
                self._on_sheriff_command_status_changed)
        self.sheriff.connect ("command-group-changed",
                self._on_sheriff_command_group_changed)
        gobject.timeout_add (1000, self._maybe_send_orders)

        self.group_row_references = {}

        self.lc.subscribe ("PMD_PRINTF", self.on_procman_printf)
        self.lc.subscribe ("PMD_ORDERS", self.on_procman_orders)

        # setup GUI
        self.window = gtk.Window (gtk.WINDOW_TOPLEVEL)
        self.window.set_default_size (800, 600)
        self.window.connect ("delete-event", gtk.main_quit)
        self.window.connect ("destroy-event", gtk.main_quit)

        vbox = gtk.VBox ()
        self.window.add (vbox)

        # keyboard accelerators.  This probably isn't the right way to do it...
        self.accel_group = gtk.AccelGroup ()
        self.accel_group.connect_group (ord("n"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, lambda *a: None)
        self.accel_group.connect_group (ord("s"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, lambda *a: None)
        self.accel_group.connect_group (ord("t"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, lambda *a: None)
        self.accel_group.connect_group (ord("e"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, lambda *a: None)
        self.accel_group.connect_group (ord("q"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, gtk.main_quit)
        self.accel_group.connect_group (ord("o"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, lambda *a: None)
        self.accel_group.connect_group (ord("a"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, 
                lambda *a: self.cmds_tv.get_selection ().select_all ())
        self.accel_group.connect_group (ord("d"), gtk.gdk.CONTROL_MASK,
                gtk.ACCEL_VISIBLE, 
                lambda *a: self.cmds_tv.get_selection ().unselect_all ())
#        self.accel_group.connect_group (ord("a"), gtk.gdk.CONTROL_MASK,
#                gtk.ACCEL_VISIBLE, self._do_save_config_dialog)
        self.accel_group.connect_group (gtk.gdk.keyval_from_name ("Delete"), 0,
                gtk.ACCEL_VISIBLE, self._remove_selected_commands)
        self.window.add_accel_group (self.accel_group)

        # setup the menu bar
        menu_bar = gtk.MenuBar ()
        vbox.pack_start (menu_bar, False, False, 0)

        file_mi = gtk.MenuItem ("_File")
        options_mi = gtk.MenuItem ("_Options")
        commands_mi = gtk.MenuItem ("_Commands")
        view_mi = gtk.MenuItem ("_View")
        
        # file menu
        file_menu = gtk.Menu ()
        file_mi.set_submenu (file_menu)

        self.load_cfg_mi = gtk.ImageMenuItem (gtk.STOCK_OPEN)
        self.load_cfg_mi.add_accelerator ("activate", self.accel_group, 
                ord("o"), gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.save_cfg_mi = gtk.ImageMenuItem (gtk.STOCK_SAVE_AS)
        quit_mi = gtk.ImageMenuItem (gtk.STOCK_QUIT)
        quit_mi.add_accelerator ("activate", self.accel_group, ord("q"),
                gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        file_menu.append (self.load_cfg_mi)
        file_menu.append (self.save_cfg_mi)
        file_menu.append (quit_mi)
        self.load_cfg_mi.connect ("activate", self._do_load_config_dialog)
        self.save_cfg_mi.connect ("activate", self._do_save_config_dialog)
        quit_mi.connect ("activate", gtk.main_quit)

        # load, save dialogs
        self.load_dlg = None
        self.save_dlg = None
        self.load_save_dir = None
        if BUILD_PREFIX and os.path.exists("%s/data/procman" % BUILD_PREFIX):
            self.load_save_dir = "%s/data/procman" % BUILD_PREFIX

        # commands menu
        commands_menu = gtk.Menu ()
        commands_mi.set_submenu (commands_menu)
        self.start_cmd_mi = gtk.MenuItem ("_Start")
        self.start_cmd_mi.add_accelerator ("activate", 
                self.accel_group, ord("s"),
                gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.start_cmd_mi.connect ("activate", self._start_selected_commands)
        self.start_cmd_mi.set_sensitive (False)
        commands_menu.append (self.start_cmd_mi)

        self.stop_cmd_mi = gtk.MenuItem ("S_top")
        self.stop_cmd_mi.add_accelerator ("activate", 
                self.accel_group, ord("t"),
                gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.stop_cmd_mi.connect ("activate", self._stop_selected_commands)
        self.stop_cmd_mi.set_sensitive (False)
        commands_menu.append (self.stop_cmd_mi)

        self.restart_cmd_mi = gtk.MenuItem ("R_estart")
        self.restart_cmd_mi.add_accelerator ("activate",
                self.accel_group, ord ("e"),
                gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.restart_cmd_mi.connect ("activate", 
                self._restart_selected_commands)
        self.restart_cmd_mi.set_sensitive (False)
        commands_menu.append (self.restart_cmd_mi)

        self.remove_cmd_mi = gtk.MenuItem ("_Remove")
        self.remove_cmd_mi.add_accelerator ("activate", self.accel_group, 
                gtk.gdk.keyval_from_name ("Delete"), 0, gtk.ACCEL_VISIBLE)
        self.remove_cmd_mi.connect ("activate", self._remove_selected_commands)
        self.remove_cmd_mi.set_sensitive (False)
        commands_menu.append (self.remove_cmd_mi)

        commands_menu.append (gtk.SeparatorMenuItem ())

        self.new_cmd_mi = gtk.MenuItem ("_New command")
        self.new_cmd_mi.add_accelerator ("activate", self.accel_group, ord("n"),
                gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.new_cmd_mi.connect ("activate", self._do_add_command_dialog)
        commands_menu.append (self.new_cmd_mi)

        # options menu
        options_menu = gtk.Menu ()
        options_mi.set_submenu (options_menu)

        self.is_observer_cmi = gtk.CheckMenuItem ("_Observer")
        self.is_observer_cmi.connect ("activate", self.on_observer_mi_activate)
        options_menu.append (self.is_observer_cmi)

        self.spawn_deputy_cmi = gtk.MenuItem("Spawn Local _Deputy")
        self.spawn_deputy_cmi.connect("activate", self.on_spawn_deputy_activate)
        options_menu.append(self.spawn_deputy_cmi)

        self.terminate_spawned_deputy_cmi = gtk.MenuItem("_Terminate local deputy")
        self.terminate_spawned_deputy_cmi.connect("activate", self.on_terminate_spawned_deputy_activate)
        options_menu.append(self.terminate_spawned_deputy_cmi)
        self.terminate_spawned_deputy_cmi.set_sensitive(False)

        self.bot_procman_deputy_cmd = find_bot_procman_deputy_cmd()
        if not self.bot_procman_deputy_cmd:
            sys.stderr.write("Can't find bot-procman-deputy.  Spawn Deputy disabled")
            self.spawn_deputy_cmi.set_sensitive(False)

        # view menu
        view_menu = gtk.Menu ()
        view_mi.set_submenu (view_menu)

        menu_bar.append (file_mi)
        menu_bar.append (options_mi)
        menu_bar.append (commands_mi)
        menu_bar.append (view_mi)

        vpane = gtk.VPaned ()
        vbox.pack_start (vpane, True, True, 0)

        # setup the command treeview
        hpane = gtk.HPaned ()
        vpane.add1 (hpane)
        
        self.cmds_ts = gtk.TreeStore (gobject.TYPE_PYOBJECT,
                gobject.TYPE_STRING, # command name
                gobject.TYPE_STRING, # command nickname
                gobject.TYPE_STRING, # host name
                gobject.TYPE_STRING, # status actual
                gobject.TYPE_STRING, # CPU usage
                gobject.TYPE_INT,    # memory vsize
                gobject.TYPE_BOOLEAN,# auto-respawn
                )

        self.cmds_tv = gtk.TreeView (self.cmds_ts)
        sw = gtk.ScrolledWindow ()
        sw.set_policy (gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        hpane.pack1 (sw, resize = True)
        sw.add (self.cmds_tv)

        cmds_tr = gtk.CellRendererText ()
        cmds_tr.set_property ("ellipsize", pango.ELLIPSIZE_END)
        plain_tr = gtk.CellRendererText ()
        status_tr = gtk.CellRendererText ()

        cols_to_make = [ \
                ("Name", cmds_tr, COL_CMDS_TV_CMD),
                ("Host", plain_tr, COL_CMDS_TV_HOST),
                ("Status", status_tr, COL_CMDS_TV_STATUS_ACTUAL),
                ("CPU %", plain_tr, COL_CMDS_TV_CPU_USAGE),
                ("Mem (kB)", plain_tr, COL_CMDS_TV_MEM_VSIZE),
                ]

        cols = []
        for name, renderer, col_id in cols_to_make:
            col = gtk.TreeViewColumn(name, renderer, text=col_id)
            col.set_sort_column_id(col_id)
            col.set_visible(self.gui_config.show_columns[col_id])
            cols.append(col)

        for col in cols:
            col.set_resizable (True)
            self.cmds_tv.append_column (col)

            name = col.get_title ()
            if name == "Name":
                continue
            col_cmi = gtk.CheckMenuItem (name)
            col_cmi.set_active (col.get_visible())
            def on_activate(cmi, col_):
                col_.set_visible(cmi.get_active())
                self.gui_config.show_columns[col_.get_sort_column_id()] = cmi.get_active()
            col_cmi.connect ("activate", on_activate, col)
            view_menu.append (col_cmi)

        cmds_sel = self.cmds_tv.get_selection ()
        cmds_sel.set_mode (gtk.SELECTION_MULTIPLE)
        cmds_sel.connect ("changed", self.on_cmds_selection_changed)

        gobject.timeout_add (1000, 
                lambda *s: self._repopulate_cmds_tv () or True)
        self.cmds_tv.add_events (gtk.gdk.KEY_PRESS_MASK | \
                gtk.gdk.BUTTON_PRESS | gtk.gdk._2BUTTON_PRESS)
        self.cmds_tv.connect ("key-press-event", 
                self._on_cmds_tv_key_press_event)
        self.cmds_tv.connect ("button-press-event", 
                self._on_cmds_tv_button_press_event)
        self.cmds_tv.connect ("row-activated",
                self._on_cmds_tv_row_activated)

        # commands treeview context menu
        self.cmd_ctxt_menu = gtk.Menu ()

        self.start_cmd_ctxt_mi = gtk.MenuItem ("_Start")
        self.cmd_ctxt_menu.append (self.start_cmd_ctxt_mi)
        self.start_cmd_ctxt_mi.connect ("activate", 
                self._start_selected_commands)

        self.stop_cmd_ctxt_mi = gtk.MenuItem ("_Stop")
        self.cmd_ctxt_menu.append (self.stop_cmd_ctxt_mi)
        self.stop_cmd_ctxt_mi.connect ("activate", self._stop_selected_commands)

        self.restart_cmd_ctxt_mi = gtk.MenuItem ("R_estart")
        self.cmd_ctxt_menu.append (self.restart_cmd_ctxt_mi)
        self.restart_cmd_ctxt_mi.connect ("activate", 
                self._restart_selected_commands)

        self.remove_cmd_ctxt_mi = gtk.MenuItem ("_Remove")
        self.cmd_ctxt_menu.append (self.remove_cmd_ctxt_mi)
        self.remove_cmd_ctxt_mi.connect ("activate", 
                self._remove_selected_commands)

        self.change_deputy_ctxt_mi = gtk.MenuItem ("_Change Host")
        self.cmd_ctxt_menu.append (self.change_deputy_ctxt_mi)
        self.change_deputy_ctxt_mi.show ()

        self.cmd_ctxt_menu.append (gtk.SeparatorMenuItem ())

        self.new_cmd_ctxt_mi = gtk.MenuItem ("_New Command")
        self.cmd_ctxt_menu.append (self.new_cmd_ctxt_mi)
        self.new_cmd_ctxt_mi.connect ("activate", self._do_add_command_dialog)

        self.cmd_ctxt_menu.show_all ()

#        # drag and drop command rows for grouping
#        dnd_targets = [ ('PROCMAN_CMD_ROW', 
#            gtk.TARGET_SAME_APP | gtk.TARGET_SAME_WIDGET, 0) ]
#        self.cmds_tv.enable_model_drag_source (gtk.gdk.BUTTON1_MASK, 
#                dnd_targets, gtk.gdk.ACTION_MOVE)
#        self.cmds_tv.enable_model_drag_dest (dnd_targets, 
#                gtk.gdk.ACTION_MOVE)

        # hosts treeview
        self.hosts_ts = gtk.ListStore (gobject.TYPE_PYOBJECT,
                gobject.TYPE_STRING, # deputy name
                gobject.TYPE_STRING, # last update time
                gobject.TYPE_STRING, # load
#                gobject.TYPE_STRING, # jitter
#                gobject.TYPE_STRING, # skew
                )

        self.hosts_tv = gtk.TreeView (self.hosts_ts)
        sw = gtk.ScrolledWindow ()
        sw.set_policy (gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        hpane.pack2 (sw, resize = False)
        sw.add (self.hosts_tv)

        col = gtk.TreeViewColumn ("Host", plain_tr, text=1)
        col.set_sort_column_id (1)
        col.set_resizable (True)
        self.hosts_tv.append_column (col)

        col = gtk.TreeViewColumn ("Last update", plain_tr, text=2)
#        col.set_sort_column_id (2) # XXX this triggers really weird bugs...
        col.set_resizable (True)
        self.hosts_tv.append_column (col)

        col = gtk.TreeViewColumn ("Load", plain_tr, text=3)
        col.set_resizable (True)
        self.hosts_tv.append_column (col)

#        col = gtk.TreeViewColumn ("Clock Skew (ms)", plain_tr, text=4)
#        col.set_resizable (True)
#        self.hosts_tv.append_column (col)
#
#        col = gtk.TreeViewColumn ("Jitter (ms)", plain_tr, text=5)
#        col.set_resizable (True)
#        self.hosts_tv.append_column (col)

        self.hosts_tv.connect ("button-press-event", 
                self._on_hosts_tv_button_press_event)

        # hosts treeview context menu
        self.hosts_ctxt_menu = gtk.Menu ()

        self.cleanup_hosts_ctxt_mi = gtk.MenuItem ("_Cleanup")
        self.hosts_ctxt_menu.append (self.cleanup_hosts_ctxt_mi)
        self.cleanup_hosts_ctxt_mi.connect ("activate", 
                self._cleanup_hosts_treeview)
        self.hosts_ctxt_menu.show_all()

        gobject.timeout_add (1000, 
                lambda *s: self._repopulate_hosts_tv() or True)

        hpane.set_position (500)

        # stdout textview
        self.stdout_textview = gtk.TextView ()
        self.stdout_textview.set_property ("editable", False)
        self.sheriff_tb = self.stdout_textview.get_buffer ()
        sw = gtk.ScrolledWindow ()
        sw.add (self.stdout_textview)
        vpane.add2 (sw)
        vpane.set_position (300)
        
        stdout_adj = sw.get_vadjustment ()
        stdout_adj.set_data ("scrolled-to-end", 1)
        stdout_adj.connect ("changed", self.on_adj_changed)
        stdout_adj.connect ("value-changed", self.on_adj_value_changed)
        
        #add callback so we can add a clear option to the default right click popup
        self.stdout_textview.connect ("populate-popup", self.on_tb_populate_menu)

        font_desc = pango.FontDescription ("Monospace")
        self.stdout_textview.modify_font (font_desc)

        # stdout rate limit maintenance events
        gobject.timeout_add (500, self._stdout_rate_limit_upkeep)

        # status bar
        self.statusbar = gtk.Statusbar ()
        vbox.pack_start (self.statusbar, False, False, 0)

        self.text_tags = { "normal" : gtk.TextTag("normal") }
        for tt in self.text_tags.values():
            self.sheriff_tb.get_tag_table().add(tt)

        vbox.show_all ()
        self.window.show ()

    def cleanup(self):
        self._terminate_spawned_deputy()
        self.gui_config.save()

    def _terminate_spawned_deputy(self):
        if self.spawned_deputy:
            try:
                self.spawned_deputy.terminate()
            except AttributeError: # python 2.4, 2.5 don't have Popen.terminate()
                os.kill(self.spawned_deputy.pid, signal.SIGTERM)
                self.spawned_deputy.wait()
        self.spawned_deputy = None

    def _get_selected_commands (self):
        selection = self.cmds_tv.get_selection ()
        if selection is None: return []
        model, rows = selection.get_selected_rows ()
        col = COL_CMDS_TV_OBJ
        selected = []
        for path in rows:
            cmds_iter = model.get_iter (path)
            cmd = model.get_value (cmds_iter, col)
            if not cmd:
                child_iter = model.iter_children (cmds_iter)
                while child_iter:
                    selected.append (model.get_value (child_iter, col))
                    child_iter = model.iter_next (child_iter)
            else:
                selected.append (cmd)
        return selected
    
    def _get_selected_hosts (self):
        model, rows = self.hosts_tv.get_selection ().get_selected_rows ()
        return [ model.get_value (model.get_iter(path), 0) \
                for path in rows ]

    def _find_or_make_group_row_reference (self, group_name):
        if not group_name:
            return None
        if group_name in self.group_row_references:
            return self.group_row_references[group_name]
        else:
            # add the group name to the command name column if visible
            # otherwise, add it to the nickname column
            ts_iter = self.cmds_ts.append (None, 
                      ((None, group_name, "", "", "", "", 0, False)))

            trr = gtk.TreeRowReference (self.cmds_ts, 
                    self.cmds_ts.get_path (ts_iter))
            self.group_row_references[group_name] = trr
            return trr

    def _get_known_group_names (self):
        return self.group_row_references.keys ()

    def _delete_group_row_reference (self, group_name):
        del self.group_row_references[group_name]

    def _repopulate_hosts_tv (self):
        to_update = set(self.sheriff.get_deputies ())
        to_remove = []

        def _deputy_last_update_str (dep):
            if dep.last_update_utime:
                now = timestamp_now ()
                return "%.1f seconds ago" % ((now-dep.last_update_utime)*1e-6)
            else:
                return "<never>"

        def _update_host_row (model, path, model_iter, user_data):
            deputy = model.get_value (model_iter, COL_HOSTS_TV_OBJ)
            if deputy in to_update:
                model.set (model_iter, 
                        COL_HOSTS_TV_LAST_UPDATE, 
                        _deputy_last_update_str (deputy),
                        COL_HOSTS_TV_LOAD, 
                        "%f" % deputy.cpu_load,
#                        COL_HOSTS_TV_SKEW, 
#                        "%f" % (deputy.clock_skew_usec * 1e-3),
#                        COL_HOSTS_TV_JITTER, 
#                        "%f" % (deputy.last_orders_jitter_usec * 1e-3),
                        )
                to_update.remove (deputy)
            else:
                to_remove.append (gtk.TreeRowReference (model, path))

        self.hosts_ts.foreach (_update_host_row, None)

        for trr in to_remove:
            self.hosts_ts.remove (self.hosts_ts.get_iter (trr.get_path()))

        for deputy in to_update:
            print "adding %s to treeview" % deputy.name
            new_row = (deputy, deputy.name, _deputy_last_update_str (deputy),
                    "%f" % deputy.cpu_load,
#                    "%f" % (deputy.clock_skew_usec * 1e-3),
#                    "%f" % (deputy.last_orders_jitter_usec * 1e-3),
                    )
            self.hosts_ts.append (new_row)

    def _repopulate_cmds_tv (self):
        now = timestamp_now ()
        if now < self.next_cmds_update_time:
            return

#        selected_cmds = self._get_selected_commands ()

        cmds = set()
        cmd_deps = {}
        deputies = self.sheriff.get_deputies ()
        for deputy in deputies:
            for cmd in deputy.get_commands ():
                cmd_deps [cmd] = deputy
                cmds.add (cmd)
        to_remove = []
        to_reparent = []

        def _update_cmd_row (model, path, model_iter, user_data):
            obj_col = COL_CMDS_TV_OBJ
            cmd = model.get_value (model_iter, obj_col)
            if not cmd: 
                # row represents a procman group
                
                # get a list of all the row's children
                child_iter = model.iter_children (model_iter)
                children = []
                while child_iter:
                    children.append (model.get_value (child_iter, obj_col))
                    child_iter = model.iter_next (child_iter)

                if not children: 
                    to_remove.append (gtk.TreeRowReference (model, path))
                    return
                statuses = [ cmd.status () for cmd in children ]
                if all ([s == statuses[0] for s in statuses]):
                    status_str = statuses[0]
                else:
                    status_str = "Mixed"
                cpu_total = sum ([cmd.cpu_usage for cmd in children])
                mem_total = sum ([cmd.mem_vsize_bytes / 1024 \
                        for cmd in children])
                cpu_str = "%.2f" % (cpu_total * 100)
                
                model.set (model_iter, 
                        COL_CMDS_TV_STATUS_ACTUAL, status_str,
                        COL_CMDS_TV_CPU_USAGE, cpu_str,
                        COL_CMDS_TV_MEM_VSIZE, mem_total)

                cur_grpname = \
                        model.get_value(model_iter, COL_CMDS_TV_CMD)

                if not cur_grpname:
                    # add the group name to the command name column
                    model.set (model_iter, 
                               COL_CMDS_TV_CMD, cmd.group)
                return
            if cmd in cmds:
#                extradata = cmd.get_data ("extradata")
                cpu_str = "%.2f" % (cmd.cpu_usage * 100)
                mem_usage = int (cmd.mem_vsize_bytes / 1024)

                name = cmd.name
                if cmd.nickname.strip():
                    name = cmd.nickname

                model.set (model_iter, 
                        COL_CMDS_TV_CMD, name,
                        COL_CMDS_TV_NICKNAME, cmd.nickname,
                        COL_CMDS_TV_STATUS_ACTUAL, cmd.status (),
                        COL_CMDS_TV_HOST, cmd_deps[cmd].name,
                        COL_CMDS_TV_CPU_USAGE, cpu_str,
                        COL_CMDS_TV_MEM_VSIZE, mem_usage,
                        COL_CMDS_TV_AUTO_RESPAWN, cmd.auto_respawn)

                # check that the command is in the correct group in the
                # treemodel
                correct_grr = self._find_or_make_group_row_reference (cmd.group)
                correct_parent_iter = None
                correct_parent_path = None
                actual_parent_path = None
                if correct_grr:
                    correct_parent_iter = model.get_iter(correct_grr.get_path())
                actual_parent_iter = model.iter_parent(model_iter)

                if correct_parent_iter:
                    correct_parent_path = model.get_path(correct_parent_iter)
                if actual_parent_iter:
                    actual_parent_path = model.get_path(actual_parent_iter)

                if correct_parent_path != actual_parent_path:
                    print "moving %s (%s) (%s)" % (cmd.name,
                            correct_parent_path, actual_parent_path)
                    # schedule the command to be moved
                    to_reparent.append ((gtk.TreeRowReference (model, path),
                        correct_grr))

                cmds.remove (cmd)
            else:
                to_remove.append (gtk.TreeRowReference (model, path))

        self.cmds_ts.foreach (_update_cmd_row, None)

        # reparent rows that are in the wrong group
        for trr, newparent_rr in to_reparent:
            orig_iter = self.cmds_ts.get_iter (trr.get_path ())
            rowdata = self.cmds_ts.get (orig_iter, *range(NUM_CMDS_ROWS))
            self.cmds_ts.remove (orig_iter)

            newparent_iter = None
            if newparent_rr:
                newparent_iter = self.cmds_ts.get_iter(newparent_rr.get_path())
            self.cmds_ts.append(newparent_iter, rowdata)

        # remove rows that have been marked for deletion
        for trr in to_remove:
            cmds_iter = self.cmds_ts.get_iter (trr.get_path())
            if not self.cmds_ts.get_value (cmds_iter, 
                    COL_CMDS_TV_OBJ):
                self._delete_group_row_reference (self.cmds_ts.get_value (cmds_iter,
                    COL_CMDS_TV_CMD))
            self.cmds_ts.remove (cmds_iter)

        # remove group rows with no children
        groups_to_remove = []
        def _check_for_lonely_groups (model, path, model_iter, user_data):
            isgroup = not model.get_value(model_iter, COL_CMDS_TV_OBJ)
            if isgroup and not model.iter_has_child (model_iter): 
                groups_to_remove.append (gtk.TreeRowReference (model, path))
        self.cmds_ts.foreach (_check_for_lonely_groups, None)
        for trr in groups_to_remove:
            model_iter = self.cmds_ts.get_iter (trr.get_path())
            self._delete_group_row_reference (self.cmds_ts.get_value (model_iter,
                COL_CMDS_TV_CMD))
            self.cmds_ts.remove (model_iter)

        # create new rows for new commands
        for cmd in cmds:
            deputy = cmd_deps[cmd]
            parent = self._find_or_make_group_row_reference (cmd.group)

            new_row = (cmd,        # COL_CMDS_TV_OBJ
                cmd.name,          # COL_CMDS_TV_CMD
                cmd.nickname,      # COL_CMDS_TV_NICKNAME
                deputy.name,       # COL_CMDS_TV_HOST
                cmd.status (),     # COL_CMDS_TV_STATUS_ACTUAL
                "0",               # COL_CMDS_TV_CPU_USAGE
                0,                 # COL_CMDS_TV_MEM_VSIZE
                cmd.auto_respawn,  # COL_CMDS_TV_AUTO_RESPAWN
                )
            if parent:
                parent_iter = self.cmds_ts.get_iter (parent.get_path ())
            else:
                parent_iter = None
            model_iter = self.cmds_ts.append (parent_iter, new_row)

        self.next_cmds_update_time = \
                timestamp_now () + UPDATE_CMDS_MIN_INTERVAL_USEC

    def _set_observer (self, is_observer):
        self.sheriff.set_observer (is_observer)

        self._update_menu_item_sensitivities ()

        if is_observer: self.window.set_title ("Procman Observer")
        else: self.window.set_title ("Procman Sheriff")

        if self.is_observer_cmi != is_observer:
            self.is_observer_cmi.set_active (is_observer)

    # GTK signal handlers
    def _do_load_config_dialog (self, *args):
        if not self.load_dlg:
            self.load_dlg = gtk.FileChooserDialog ("Load Config", self.window, 
                    buttons = (gtk.STOCK_OPEN, gtk.RESPONSE_ACCEPT,
                        gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT))
        if self.load_save_dir:
            self.load_dlg.set_current_folder(self.load_save_dir)
        if gtk.RESPONSE_ACCEPT == self.load_dlg.run ():
            self.config_filename = self.load_dlg.get_filename ()
            self.load_save_dir = os.path.dirname(self.config_filename)
            try:
                cfg = sheriff_config.config_from_filename (self.config_filename)
            except Exception:
                msgdlg = gtk.MessageDialog (self.window,
                        gtk.DIALOG_MODAL|gtk.DIALOG_DESTROY_WITH_PARENT,
                        gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE, 
                        traceback.format_exc ())
                msgdlg.run ()
                msgdlg.destroy ()
            else:
                self.sheriff.load_config (cfg)
        self.load_dlg.hide()

    def _do_save_config_dialog (self, *args):
        if not self.save_dlg:
            self.save_dlg = gtk.FileChooserDialog ("Save Config", self.window,
                    action = gtk.FILE_CHOOSER_ACTION_SAVE,
                    buttons = (gtk.STOCK_SAVE, gtk.RESPONSE_ACCEPT,
                        gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT))
        if self.load_save_dir:
            self.save_dlg.set_current_folder(self.load_save_dir)
        if self.config_filename is not None:
            self.save_dlg.set_filename (self.config_filename)
        if gtk.RESPONSE_ACCEPT == self.save_dlg.run ():
            self.config_filename = self.save_dlg.get_filename ()
            self.load_save_dir = os.path.dirname(self.config_filename)
            try:
                self.sheriff.save_config (file (self.config_filename, "w"))
            except IOError, e:
                msgdlg = gtk.MessageDialog (self.window,
                        gtk.DIALOG_MODAL|gtk.DIALOG_DESTROY_WITH_PARENT,
                        gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE, str (e))
                msgdlg.run ()
                msgdlg.destroy ()
        self.save_dlg.hide ()
        self.save_dlg.destroy()
        self.save_dlg = None

    def on_observer_mi_activate (self, menu_item):
        self._set_observer (menu_item.get_active ())

    def on_spawn_deputy_activate(self, *args):
        self._terminate_spawned_deputy()
        args = [ self.bot_procman_deputy_cmd, "-n", "localhost" ]
        self.spawned_deputy = subprocess.Popen(args)
        # TODO disable
        self.spawn_deputy_cmi.set_sensitive(False)
        self.terminate_spawned_deputy_cmi.set_sensitive(True)

    def on_terminate_spawned_deputy_activate(self, *args):
        self._terminate_spawned_deputy()
        self.spawn_deputy_cmi.set_sensitive(True)
        self.terminate_spawned_deputy_cmi.set_sensitive(False)

    def _do_add_command_dialog (self, *args):
        deputies = self.sheriff.get_deputies ()
        if not deputies:
            msgdlg = gtk.MessageDialog (self.window, 
                    gtk.DIALOG_MODAL|gtk.DIALOG_DESTROY_WITH_PARENT,
                    gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE,
                    "Can't add a command without an active deputy")
            msgdlg.run ()
            msgdlg.destroy ()
            return
        dlg = AddModifyCommandDialog (self.window, deputies,
                self._get_known_group_names ())
        while dlg.run () == gtk.RESPONSE_ACCEPT:
            cmd = dlg.get_command ()
            cmd_nickname = dlg.get_nickname()
            deputy = dlg.get_deputy ()
            group = dlg.get_group ().strip ()
            auto_respawn = dlg.get_auto_respawn ()
            if not cmd.strip ():
                msgdlg = gtk.MessageDialog (self.window, 
                        gtk.DIALOG_MODAL|gtk.DIALOG_DESTROY_WITH_PARENT,
                        gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE, "Invalid command")
                msgdlg.run ()
                msgdlg.destroy ()
            elif not deputy:
                msgdlg = gtk.MessageDialog (self.window, 
                        gtk.DIALOG_MODAL|gtk.DIALOG_DESTROY_WITH_PARENT,
                        gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE, "Invalid deputy")
                msgdlg.run ()
                msgdlg.destroy ()
            else:
                self.sheriff.add_command (deputy.name, cmd, cmd_nickname, group, auto_respawn)
                break
        dlg.destroy ()

    def _start_selected_commands (self, *args):
        for cmd in self._get_selected_commands ():
            self.sheriff.start_command (cmd)

    def _stop_selected_commands (self, *args):
        for cmd in self._get_selected_commands ():
            self.sheriff.stop_command (cmd)

    def _restart_selected_commands (self, *args):
        for cmd in self._get_selected_commands ():
            self.sheriff.restart_command (cmd)

    def _remove_selected_commands (self, *args):
        toremove = self._get_selected_commands ()
        for cmd in toremove:
            self.sheriff.schedule_command_for_removal (cmd)

    def _update_menu_item_sensitivities (self):
        # enable/disable menu options based on sheriff state and user selection
        selected_cmds = self._get_selected_commands ()
        can_modify = len(selected_cmds) > 0 and not self.sheriff.is_observer ()
        can_add_load = not self.sheriff.is_observer ()

        self.start_cmd_mi.set_sensitive (can_modify)
        self.stop_cmd_mi.set_sensitive (can_modify)
        self.restart_cmd_mi.set_sensitive (can_modify)
        self.remove_cmd_mi.set_sensitive (can_modify)

        self.new_cmd_mi.set_sensitive (can_add_load)
        self.load_cfg_mi.set_sensitive (can_add_load)

    def on_cmds_selection_changed (self, selection):
        selected_cmds = self._get_selected_commands ()
        if len (selected_cmds) == 1:
            cmd = selected_cmds[0]
            extradata = cmd.get_data ("extradata")
            self.stdout_textview.set_buffer (extradata.tb)
        elif len (selected_cmds) == 0:
            self.stdout_textview.set_buffer (self.sheriff_tb)
        self._update_menu_item_sensitivities ()

    def on_adj_changed (self, adj):
        if adj.get_data ("scrolled-to-end"):
            adj.set_value (adj.upper - adj.page_size)

    def on_adj_value_changed (self, adj):
        adj.set_data ("scrolled-to-end", adj.value == adj.upper-adj.page_size)

    def _stdout_rate_limit_upkeep (self):
        for cmd in self.sheriff.get_all_commands ():
            extradata = cmd.get_data ("extradata")
            if not extradata: continue
            if extradata.printf_drop_count:
                deputy = self.sheriff.get_command_deputy (cmd)
                self._add_text_to_buffer (extradata.tb, now_str() + 
                        "\nSHERIFF RATE LIMIT: Ignored %d bytes of output\n" %
                        (extradata.printf_drop_count))
                self._add_text_to_buffer (self.sheriff_tb, now_str() + 
                        "Ignored %d bytes of output from [%s] [%s]\n" % \
                        (extradata.printf_drop_count, deputy.name, cmd.name))

            extradata.printf_keep_count.pop (0)
            extradata.printf_keep_count.append (0)
            extradata.printf_drop_count = 0
        return True

    def _status_cell_data_func (self, column, cell, model, model_iter):
        color_map = {
                sheriff.TRYING_TO_START : "Orange",
                sheriff.RESTARTING : "Orange",
                sheriff.RUNNING : "Green",
                sheriff.TRYING_TO_STOP : "Yellow",
                sheriff.REMOVING : "Yellow",
                sheriff.STOPPED_OK : "White",
                sheriff.STOPPED_ERROR : "Red",
                sheriff.UNKNOWN : "Red"
                }

        col = COL_CMDS_TV_OBJ
        cmd = model.get_value (model_iter, col)
        if not cmd:
            # group node
            child_iter = model.iter_children (model_iter)
            children = []
            while child_iter:
                children.append (model.get_value (child_iter, col))
                child_iter = model.iter_next (child_iter)

            if not children:
                cell.set_property ("cell-background-set", False)
            else:
                cell.set_property ("cell-background-set", True)

                statuses = [ cmd.status () for cmd in children ]
                
                if all ([s == statuses[0] for s in statuses]):
                    # if all the commands in a group have the same status, then
                    # color them by that status
                    cell.set_property ("cell-background", 
                            color_map[statuses[0]])
                else:
                    # otherwise, color them yellow
                    cell.set_property ("cell-background", "Yellow")

            return

        cell.set_property ("cell-background-set", True)
        cell.set_property ("cell-background", color_map[cmd.status ()])

    def _maybe_send_orders (self):
        if not self.sheriff.is_observer (): self.sheriff.send_orders ()
        return True

    def _on_cmds_tv_key_press_event (self, widget, event):
        if event.keyval == gtk.gdk.keyval_from_name ("Right"):
            # expand a group row when user presses right arrow key
            model, rows = self.cmds_tv.get_selection ().get_selected_rows ()
            if len (rows) == 1:
#                col = COL_CMDS_TV_OBJ
                model_iter = model.get_iter (rows[0])
                if model.iter_has_child (model_iter):
                    self.cmds_tv.expand_row (rows[0], True)
                return True
        elif event.keyval == gtk.gdk.keyval_from_name ("Left"):
            # collapse a group row when user presses left arrow key
            model, rows = self.cmds_tv.get_selection ().get_selected_rows ()
            if len (rows) == 1:
#                col = COL_CMDS_TV_OBJ
                model_iter = model.get_iter (rows[0])
                if model.iter_has_child (model_iter):
                    self.cmds_tv.collapse_row (rows[0])
                else:
                    parent = model.iter_parent (model_iter)
                    if parent:
                        parent_path = self.cmds_ts.get_path (parent)
                        self.cmds_tv.set_cursor (parent_path)
                return True
        return False

    def _on_cmds_tv_button_press_event (self, treeview, event):
        if event.type == gtk.gdk.BUTTON_PRESS and event.button == 3:
            time = event.time
            treeview.grab_focus ()
            sel = self.cmds_tv.get_selection ()
            model, rows = sel.get_selected_rows ()
            pathinfo = treeview.get_path_at_pos (int (event.x), int (event.y))

            if pathinfo is not None:
                if pathinfo[0] not in rows:
                    # if user right-clicked on a previously unselected row,
                    # then unselect all other rows and select only the row
                    # under the mouse cursor
                    path, col, cellx, celly = pathinfo
                    treeview.grab_focus ()
                    treeview.set_cursor (path, col, 0)

                # build a submenu of all deputies
#                selected_cmds = self._get_selected_commands ()
#                can_start_stop_remove = len(selected_cmds) > 0 and \
#                        not self.sheriff.is_observer ()

                deputy_submenu = gtk.Menu ()
                deps = [ (d.name, d) for d in self.sheriff.get_deputies () ]
                deps.sort ()
                for name, deputy in deps:
                    dmi = gtk.MenuItem (name)
                    deputy_submenu.append (dmi)
                    dmi.show ()

                    def _onclick (mi, newdeputy):
                        for cmd in self._get_selected_commands ():
                            old_dep = self.sheriff.get_command_deputy (cmd)

                            if old_dep == newdeputy: continue

                            self.sheriff.move_command_to_deputy(cmd, newdeputy)

                    dmi.connect ("activate", _onclick, deputy)

                self.change_deputy_ctxt_mi.set_submenu (deputy_submenu)
            else:
                sel.unselect_all ()

            # enable/disable menu options based on sheriff state and user
            # selection
            can_add_load = not self.sheriff.is_observer ()
            can_modify = pathinfo is not None and not self.sheriff.is_observer()

            self.start_cmd_ctxt_mi.set_sensitive (can_modify)
            self.stop_cmd_ctxt_mi.set_sensitive (can_modify)
            self.restart_cmd_ctxt_mi.set_sensitive (can_modify)
            self.remove_cmd_ctxt_mi.set_sensitive (can_modify)
            self.change_deputy_ctxt_mi.set_sensitive (can_modify)
            self.new_cmd_ctxt_mi.set_sensitive (can_add_load)

            self.cmd_ctxt_menu.popup (None, None, None, event.button, time)
            return 1
        elif event.type == gtk.gdk._2BUTTON_PRESS and event.button == 1:
            # expand or collapse groups when double clicked
            sel = self.cmds_tv.get_selection ()
            model, rows = sel.get_selected_rows ()
            if len (rows) == 1:
                if model.iter_has_child (model.get_iter (rows[0])):
                    if self.cmds_tv.row_expanded (rows[0]):
                        self.cmds_tv.collapse_row (rows[0])
                    else:
                        self.cmds_tv.expand_row (rows[0], True)
        elif event.type == gtk.gdk.BUTTON_PRESS and event.button == 1:
            # unselect all rows when the user clicks on empty space in the
            # commands treeview
            time = event.time
            x = int (event.x)
            y = int (event.y)
            pathinfo = treeview.get_path_at_pos (x, y)
            if pathinfo is None:
                self.cmds_tv.get_selection ().unselect_all ()
                
    def _on_hosts_tv_button_press_event (self, treeview, event):
        if event.type == gtk.gdk.BUTTON_PRESS and event.button == 3:
            self.hosts_ctxt_menu.popup (None, None, None, event.button, event.time)
            return True

    def _cleanup_hosts_treeview(self, *args):
        self.sheriff.purge_useless_deputies()
        self._repopulate_hosts_tv()

    def _on_cmds_tv_row_activated (self, treeview, path, column):
        model_iter = self.cmds_ts.get_iter (path)
        cmd = self.cmds_ts.get_value (model_iter, COL_CMDS_TV_OBJ)
        if not cmd:
            return

        old_deputy = self.sheriff.get_command_deputy (cmd)
        dlg = AddModifyCommandDialog (self.window, 
                self.sheriff.get_deputies (),
                self._get_known_group_names (),
                cmd.name, cmd.nickname, old_deputy, 
                cmd.group, cmd.auto_respawn)
        if dlg.run () == gtk.RESPONSE_ACCEPT:
            newname = dlg.get_command ()
            newnickname = dlg.get_nickname ()
            newdeputy = dlg.get_deputy ()
            newgroup = dlg.get_group ().strip ()
            newauto_respawn = dlg.get_auto_respawn ()

            if newname != cmd.name:
                self.sheriff.set_command_name (cmd, newname)

            if newnickname != cmd.nickname:
                self.sheriff.set_command_nickname (cmd, newnickname)

            if newauto_respawn != cmd.auto_respawn:
                self.sheriff.set_auto_respawn (cmd, newauto_respawn)

            if newdeputy != old_deputy:
                self.sheriff.move_command_to_deputy(cmd, newdeputy)

            if newgroup != cmd.group:
                self.sheriff.set_command_group (cmd, newgroup)
        dlg.destroy ()

    # Sheriff event handlers
    def _on_sheriff_command_added (self, sheriff, deputy, command):
        extradata = CommandExtraData (self.sheriff_tb.get_tag_table())
        command.set_data ("extradata", extradata)
        self._add_text_to_buffer (self.sheriff_tb, now_str() + 
                "Added [%s] [%s]\n" % (deputy.name, command.name))
        self._repopulate_cmds_tv ()

    def _on_sheriff_command_removed (self, sheriff, deputy, command):
        self._add_text_to_buffer (self.sheriff_tb, now_str() + 
                "[%d] removed (%s:%s)\n" % (command.sheriff_id,
                deputy.name, command.name))
        self._repopulate_cmds_tv ()

    def _on_sheriff_command_status_changed (self, sheriff, cmd,
            old_status, new_status):
        self._add_text_to_buffer (self.sheriff_tb,now_str() + 
                "[%s] new status: %s\n" % (cmd.name, new_status))
        self._repopulate_cmds_tv ()

    def _on_sheriff_command_group_changed (self, sheriff, cmd):
        self._repopulate_cmds_tv ()

    def _tag_from_seg(self, seg):
        esc_seq, seg = seg.split("m", 1)
        if not esc_seq:
            esc_seq = "0"
        key = esc_seq
        codes = esc_seq.split(";")
        if len(codes) > 0:
            codes.sort()
            key = ";".join(codes)
        if key not in self.text_tags:
            tag = gtk.TextTag(key)
            for code in codes:
                if code in ANSI_CODES_TO_TEXT_TAG_PROPERTIES:
                    propname, propval = ANSI_CODES_TO_TEXT_TAG_PROPERTIES[code]
                    tag.set_property(propname, propval)
            self.sheriff_tb.get_tag_table().add(tag)
            self.text_tags[key] = tag
        return self.text_tags[key], seg

    def _add_text_to_buffer (self, tb, text):
        if not text:
            return

        # interpret text as ANSI escape sequences?  Try to format colors...
        tag = self.text_tags["normal"]
        for segnum, seg in enumerate(text.split("\x1b[")):
            if not seg:
                continue
            if segnum > 0:
                tag, seg = self._tag_from_seg(seg)
            end_iter = tb.get_end_iter()
            tb.insert_with_tags(end_iter, seg, tag)

        # toss out old text if the muffer is getting too big
        num_lines = tb.get_line_count ()
        if num_lines > self.stdout_maxlines:
            start_iter = tb.get_start_iter ()
            chop_iter = tb.get_iter_at_line (num_lines - self.stdout_maxlines)
            tb.delete (start_iter, chop_iter)
            
    def on_tb_populate_menu(self,textview, menu):
        sep = gtk.SeparatorMenuItem()
        menu.append (sep)
        sep.show()
        mi = gtk.MenuItem ("_Clear")
        menu.append(mi)
        mi.connect ("activate", self._tb_clear)
        mi.show()

    def _tb_clear(self,menu):
        tb = self.stdout_textview.get_buffer ()
        start_iter = tb.get_start_iter ()
        end_iter = tb.get_end_iter ()
        tb.delete (start_iter, end_iter)

    # LCM handlers
    def on_procman_orders (self, channel, data):
        msg = orders_t.decode (data)
        if not self.sheriff.is_observer () and \
                self.sheriff.name != msg.sheriff_name:
            # detected the presence of another sheriff that is not this one.
            # self-demote to prevent command thrashing
            self._set_observer (True)

            self.statusbar.push (self.statusbar.get_context_id ("main"),
                    "WARNING: multiple sheriffs detected!  Switching to observer mode");
            gobject.timeout_add (6000, 
                    lambda *s: self.statusbar.pop (self.statusbar.get_context_id ("main")))

    def on_procman_printf (self, channel, data):
        msg = printf_t.decode (data)
        if msg.sheriff_id:
            try:
                cmd = self.sheriff.get_command_by_id (msg.sheriff_id)
            except KeyError:
                # TODO
                return

            extradata = cmd.get_data ("extradata")
            if not extradata: return

            # rate limit
            msg_count = sum (extradata.printf_keep_count)
            if msg_count >= PRINTF_RATE_LIMIT:
                extradata.printf_drop_count += len (msg.text)
                return

            tokeep = min (PRINTF_RATE_LIMIT - msg_count, len (msg.text))
            extradata.printf_keep_count[-1] += tokeep

            if len (msg.text) > tokeep:
                toadd = msg.text[:tokeep]
            else:
                toadd = msg.text

            self._add_text_to_buffer (extradata.tb, toadd)

def usage():
    sys.stdout.write(
"""usage: %s [options] <procman_config_file>

Process Management operating console.

Options:
  -l, --lone-ranger   Automatically run a deputy within the sheriff process
                      This deputy terminates with the sheriff, along with
                      all the commands it hosts.

  -h, --help          Shows this help text

If <procman_config_file> is specified, then the sheriff tries to load
deputy commands from the file.

""" % os.path.basename(sys.argv[0]))
    sys.exit(1)

def run ():
    try:
        opts, args = getopt.getopt( sys.argv[1:], 'hl',
                ['help','lone-ranger'] )
    except getopt.GetoptError:
        usage()
        sys.exit(2)

    spawn_deputy = False

    for optval, argval in opts:
        if optval in [ '-l', '--lone-ranger' ]:
            spawn_deputy = True
        elif optval in [ '-h', '--help' ]:
            usage()

    cfg = None
    if len(args) > 0:
        cfg = sheriff_config.config_from_filename(args[0])

    lc = LCM ()
    def handle (*a):
        try:
            lc.handle ()
        except Exception:
            traceback.print_exc ()
        return True
    gobject.io_add_watch (lc, gobject.IO_IN, handle)
    gui = SheriffGtk(lc)
    if spawn_deputy:
        gui.on_spawn_deputy_activate()
    if cfg is not None:
        gobject.timeout_add (2000, lambda: gui.sheriff.load_config (cfg))
        gui.load_save_dir = os.path.dirname(args[0])
    try:
        gtk.main ()
    except KeyboardInterrupt:
        print("Exiting")
    gui.cleanup()

if __name__ == "__main__":
    run ()

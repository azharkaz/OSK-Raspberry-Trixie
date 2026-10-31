#!/usr/bin/env python3
"""patch_keyboard.py - наши правки wvkbd (keyboard.c и keyboard.h), безопасно запускать повторно.

Запуск из каталога wvkbd:  python3 patch_keyboard.py
Перед первым изменением сохраняет keyboard.c.orig и keyboard.h.orig.
Каждая правка применяется только если все её шаблоны найдены, иначе файлы не трогаются.

Правки:
  1. caps       метки кириллических букв тоже становятся заглавными при Caps
                (в оригинале проверка strlen(label) == 1 подходит только латинице).
  2. mod-lock   залипание Shift/Ctrl/Alt/Win/AltGr: двойное быстрое нажатие (< 400 мс)
                фиксирует модификатор, простое нажатие снимает. При залипшем Shift
                пробел остаётся пробелом (а не Tab).
  3. hold       у клавиши можно задать действие на удержание (>= 450 мс):
                hold_code/hold_mod (комбинация) или hold_cmd (команда оболочки).
                Такая клавиша срабатывает при отпускании: короткое нажатие - обычное
                действие, долгое - действие на удержание.
  4. layout-sync синхронизация раскладки экранной клавиатуры с системной (main.c и keyboard.c):
                - сигнал SIGRTMIN+N выбирает N-й слой из списка слоёв (-l wideen,wideru):
                  SIGRTMIN+1 = первый (EN), SIGRTMIN+2 = второй (RU); слой меняется и при
                  скрытой клавиатуре, управляющие сигналы блокируются с самого старта;
                - нажатие кнопки раскладки на экране запускает команду из $OSK_LAYOUT_CMD,
                  ей передаётся имя раскладки (например wideru): по ней система меняет язык.
"""
import re
import shutil
import sys
from pathlib import Path

KC, KH, KM = Path("keyboard.c"), Path("keyboard.h"), Path("main.c")
M_CAPS = "/* osk-patch: caps_affects */"
M_LOCK = "/* osk-patch: mod-lock+hold */"
M_SYNC = "/* osk-patch: layout-sync */"

CAPS_HELPER = M_CAPS + r'''
static int caps_affects(const char *label) {
    unsigned char c = (unsigned char)label[0];
    if (c == 0)
        return 0;
    if (c < 0x80)                              /* одна латинская буква */
        return label[1] == 0 && isalpha(c);
    /* одна кириллическая буква (U+0400..U+04FF): байты D0/D1 + ещё один */
    return (c == 0xD0 || c == 0xD1) && label[1] != 0 && label[2] == 0;
}
'''

LOCK_PROTO = M_LOCK + r'''
#include <signal.h>
#include <unistd.h>
#define OSK_HOLD_MS 450
#define OSK_DBLTAP_MS 400
static void osk_fire_hold(struct kbd *kb, uint32_t time);
static int osk_mod_double_tap(struct kbd *kb, struct key *k, uint32_t time);
'''

LOCK_BODIES = r'''/* ---- osk-patch: залипание модификаторов и действия на удержание ---- */
static void osk_run(const char *cmd)
{
    static int inited;
    if (!inited) {
        signal(SIGCHLD, SIG_IGN);
        inited = 1;
    }
    if (fork() == 0) {
        setsid();
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
}

static void osk_send_combo(struct kbd *kb, uint32_t time, uint32_t mod, uint32_t code)
{
    uint32_t add = mod & ~kb->mods;      /* модификаторы, которых ещё нет в нажатых */
    if (add) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time, add, WL_KEYBOARD_KEY_STATE_PRESSED);
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods | mod, 0, 0, 0);
    }
    zwp_virtual_keyboard_v1_key(kb->vkbd, time + 1, code, WL_KEYBOARD_KEY_STATE_PRESSED);
    zwp_virtual_keyboard_v1_key(kb->vkbd, time + 2, code, WL_KEYBOARD_KEY_STATE_RELEASED);
    if (add) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time + 3, add, WL_KEYBOARD_KEY_STATE_RELEASED);
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods, 0, 0, 0);
    }
}

static void osk_fire_hold(struct kbd *kb, uint32_t time)
{
    struct key *k = kb->hold_pending;
    uint32_t un;
    int held = (uint32_t)(time - kb->press_time) >= OSK_HOLD_MS;
    kb->hold_pending = NULL;
    kb->last_press = NULL;
    if (held && k->hold_cmd)
        osk_run(k->hold_cmd);
    else if (held && k->hold_code)
        osk_send_combo(kb, time, k->hold_mod, k->hold_code);
    else
        osk_send_combo(kb, time, k->code_mod, k->code);
    /* как у обычных клавиш: одноразовые модификаторы снимаются после нажатия */
    un = kb->mods & ~kb->locked_mods & (Shift | Ctrl | Alt | Super | AltGr);
    if (un) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time + 4, un, WL_KEYBOARD_KEY_STATE_RELEASED);
        kb->mods &= ~un;
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods, 0, 0, 0);
    }
    kbd_draw_key(kb, k, Unpress);
    if (un & Shift)
        kbd_draw_layout(kb);
}

static int osk_mod_double_tap(struct kbd *kb, struct key *k, uint32_t time)
{
    uint32_t m = k->code;
    int consumed = 0;
    kb->locked_mods &= kb->mods;
    if (m == Shift || m == Ctrl || m == Alt || m == Super || m == AltGr) {
        if (kb->locked_mods & m) {
            kb->locked_mods &= ~m;                 /* простое нажатие снимает залипание */
        } else if ((kb->mods & m) && kb->last_mod_code == m &&
                   (uint32_t)(time - kb->last_mod_time) < OSK_DBLTAP_MS) {
            kb->locked_mods |= m;                  /* двойное быстрое нажатие: залипание */
            consumed = 1;
        }
    }
    kb->last_mod_code = m;
    kb->last_mod_time = time;
    return consumed;
}

'''

HOLD_HOOK = r'''        if (k->hold_code || k->hold_cmd) {     /* osk-patch: срабатывает при отпускании */
            kb->last_swipe = kb->last_press = k;
            kb->last_press_offset = time - monotime();
            kb->hold_pending = k;
            kb->press_time = time;
            kbd_draw_key(kb, k, Press);
            break;
        }
'''

CAPS_RE = re.compile(r"strlen\(\s*k->label\s*\)\s*==\s*1\s*&&\s*isalpha\(\s*k->label\[0\]\s*\)")


def sub1(text, pattern, repl, what, count=1, hint=None):
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        msg = f"«{what}»: найдено {n} совпадений, ожидалось {count}"
        if hint:
            near = [ln for ln in text.splitlines() if hint in ln][:3]
            msg += f"\nСтроки с «{hint}» в файле (repr): {near}"
        raise ValueError(msg)
    return new


def patch_caps(c):
    if M_CAPS in c:
        return c, False
    n = len(CAPS_RE.findall(c))
    if n < 1:
        raise ValueError("«caps»: не нашёл проверку isalpha(label)")
    c = CAPS_RE.sub("caps_affects(k->label)", c)
    inc = list(re.finditer(r"^#include[^\n]*\n", c, re.M))
    if not inc:
        raise ValueError("«caps»: нет #include в keyboard.c")
    pos = inc[-1].end()
    return c[:pos] + "\n" + CAPS_HELPER + c[pos:], True


def patch_lock(c, h):
    if M_LOCK in c:
        return c, h, False
    # --- keyboard.h ---
    h = sub1(h, r"^([ \t]*)(bool reset_mod;[^\n]*\n)",
             lambda m: m.group(1) + m.group(2) +
             f"{m.group(1)}uint32_t hold_code;      /* osk: клавиша для удержания (0 - нет) */\n"
             f"{m.group(1)}uint32_t hold_mod;       /* osk: модификатор для удержания */\n"
             f"{m.group(1)}const char *hold_cmd;    /* osk: команда оболочки на удержание */\n",
             "struct key", hint="reset_mod")
    h = sub1(h, r"(struct kbd \{\n)([ \t]*)(bool debug;\n)",
             lambda m: m.group(1) + m.group(2) + m.group(3) +
             f"{m.group(2)}uint32_t locked_mods;    /* osk: залипшие модификаторы */\n"
             f"{m.group(2)}uint32_t last_mod_code, last_mod_time, press_time;\n"
             f"{m.group(2)}struct key *hold_pending;\n",
             "struct kbd", hint="struct kbd")
    # --- keyboard.c ---
    inc = list(re.finditer(r"^#include[^\n]*\n", c, re.M))
    if not inc:
        raise ValueError("«mod-lock»: нет #include в keyboard.c")
    pos = inc[-1].end()
    c = c[:pos] + "\n" + LOCK_PROTO + c[pos:]
    c = sub1(c, r"(unlatch_\w+ = \(kb->mods & (Shift|Ctrl|AltGr|Alt|Super)\) == \2)(;)",
             lambda m: f"{m.group(1)} && !(kb->locked_mods & {m.group(2)}){m.group(3)}",
             "unlatch", count=5, hint="unlatch_shift =")
    c = sub1(c, r"(void\nkbd_unpress_key\(struct kbd \*kb, uint32_t time\)\n\{\n)",
             lambda m: m.group(1) + "    if (kb->hold_pending)\n        osk_fire_hold(kb, time);\n"
                                    "    kb->locked_mods &= kb->mods;\n",
             "kbd_unpress_key", hint="kbd_unpress_key")
    c = sub1(c, r"(\nvoid\nkbd_press_key\(struct kbd \*kb, struct key \*k, uint32_t time\)\n\{)",
             lambda m: "\n" + LOCK_BODIES + m.group(1), "kbd_press_key", hint="kbd_press_key")
    c = sub1(c, r"([ \t]*case Code:\n)([ \t]*if \(k->code_mod\) \{\n)",
             lambda m: m.group(1) + HOLD_HOOK + m.group(2), "case Code", hint="case Code")
    c = sub1(c, r"([ \t]*case Mod:\n)([ \t]*kb->mods \^= k->code;\n)",
             lambda m: m.group(1) + "        if (osk_mod_double_tap(kb, k, time))\n            break;\n" + m.group(2),
             "case Mod", hint="case Mod")
    c = sub1(c, r"(\(kb->shift_space_is_tab\) && \(k->code == KEY_SPACE\) && \(kb->mods & Shift\))(\) \{)",
             lambda m: m.group(1) + " && !(kb->locked_mods & Shift)" + m.group(2),
             "shift+space", hint="shift_space_is_tab")
    return c, h, True


SYNC_KC_FUNC = M_SYNC + r"""
extern int osk_pref_layer;   /* main.c: слой, который надо вернуть при следующем показе клавиатуры */

/* Нажатие кнопки раскладки на экране: сообщаем системе командой из $OSK_LAYOUT_CMD <имя раскладки> */
static void osk_layout_changed(struct kbd *kb)
{
    const char *cmd = getenv("OSK_LAYOUT_CMD");
    char buf[512];
    enum layout_id *lst = kb->landscape ? kb->landscape_layers : kb->layers;
    int i;
    for (i = 0; i < MAX_LAYERS && lst[i] != NumLayouts; i++) {
        if (&kb->layouts[lst[i]] == kb->layout) {
            osk_pref_layer = i;
            break;
        }
    }
    if (!cmd || !*cmd || !kb->layout || !kb->layout->name)
        return;
    snprintf(buf, sizeof buf, "%s '%s'", cmd, kb->layout->name);
    osk_run(buf);
}

"""

SYNC_MAIN_EARLY = M_SYNC + r"""
    /* osk-patch: управляющие сигналы блокируем сразу при старте: пока не создан signalfd,
       «ранний» сигнал иначе завершил бы процесс */
    {
        sigset_t osk_early;
        int osk_n;
        sigemptyset(&osk_early);
        sigaddset(&osk_early, SIGUSR1);
        sigaddset(&osk_early, SIGUSR2);
        for (osk_n = 0; osk_n <= OSK_MAX_LAYER_SIGS; osk_n++)
            sigaddset(&osk_early, SIGRTMIN + osk_n);
        sigprocmask(SIG_BLOCK, &osk_early, NULL);
        signal(SIGCHLD, SIG_IGN);
    }
"""

SYNC_MAIN_PREF = M_SYNC + r"""
/* Слой, выбранный пользователем или системой. Штатный wvkbd при каждом показе клавиатуры сбрасывает слой на
   первый, из-за этого после скрытия и показа раскладка расходилась с системной. */
int osk_pref_layer = 0;

/* Метка для osk и osk-tray: по ней они узнают, что этот wvkbd понимает сигналы смены слоя
   (старый wvkbd от такого сигнала завершается, поэтому без метки сигнал не посылают). */
const char osk_layer_sync_marker[] = "OSK_LAYER_SYNC_V1";
"""

REDIM_RE = re.compile(
    r"    enum layout_id layer;\n"
    r"    if \(keyboard\.landscape\) \{\n"
    r"        layer = keyboard\.landscape_layers\[0\];\n"
    r"        height = landscape_height;\n"
    r"    \} else \{\n"
    r"        layer = keyboard\.layers\[0\];\n"
    r"        height = normal_height;\n"
    r"    \}\n")

REDIM_NEW = r"""    enum layout_id layer;
    /* osk-patch: layout-sync: при показе возвращаем выбранный слой (а не всегда первый) */
    struct layout *osk_old = keyboard.layout;
    {
        enum layout_id *osk_lst = keyboard.landscape ? keyboard.landscape_layers : keyboard.layers;
        int osk_cnt = 0;
        while (osk_cnt < MAX_LAYERS && osk_lst[osk_cnt] != NumLayouts)
            osk_cnt++;
        layer = osk_lst[(osk_pref_layer >= 0 && osk_pref_layer < osk_cnt) ? osk_pref_layer : 0];
    }
    height = keyboard.landscape ? landscape_height : normal_height;
"""

REDIM_TAIL_RE = re.compile(
    r"(    keyboard\.prevlayout = keyboard\.layout;\n"
    r"    keyboard\.last_abc_layout = keyboard\.layout;\n"
    r"    keyboard\.last_abc_index = 0;\n)\}")

REDIM_TAIL_NEW = r"""    /* osk-patch: layout-sync: подпись клавиш и раскладка ввода должны совпадать */
    if (osk_old && osk_old != keyboard.layout && keyboard.vkbd &&
        strcmp(osk_old->keymap_name, keyboard.layout->keymap_name) != 0)
        create_and_upload_keymap(&keyboard, keyboard.layout->keymap_name, 0);
}"""

SYNC_MAIN_FUNC = M_SYNC + r"""
#define OSK_MAX_LAYER_SIGS 8

/* Выбор слоя извне: сигнал SIGRTMIN+N выбирает N-й слой списка слоёв (нумерация с 1).
   Повторный выбор того же слоя ничего не делает, поэтому системе можно слать состояние сколько угодно раз. */
static void
osk_set_layer(int n)
{
    enum layout_id *list =
        keyboard.landscape ? keyboard.landscape_layers : keyboard.layers;
    int count = 0;
    struct layout *l, *prev;

    while (count < MAX_LAYERS && list[count] != NumLayouts)
        count++;
    if (n < 0 || n >= count)
        return;
    osk_pref_layer = n;
    l = &keyboard.layouts[list[n]];
    prev = keyboard.layout;
    if (prev == l)
        return;
    if (hidden || !layer_surface) {
        /* клавиатура скрыта: поверхности нет, рисовать нельзя - меняем только состояние и раскладку ввода */
        keyboard.prevlayout = prev;
        keyboard.layout = l;
        keyboard.layer_index = kbd_get_layer_index(&keyboard, l);
        if (!prev || strcmp(prev->keymap_name, l->keymap_name) != 0)
            create_and_upload_keymap(&keyboard, l->keymap_name, 0);
    } else {
        kbd_switch_layout(&keyboard, l, kbd_get_layer_index(&keyboard, l));
    }
    keyboard.compose = 0;
    keyboard.last_abc_index = 0;
    keyboard.last_abc_layout = &keyboard.layouts[list[0]];
}

"""

SYNC_MAIN_SIGNALS = M_SYNC + r"""
    for (int osk_n = 1; osk_n <= OSK_MAX_LAYER_SIGS; osk_n++)
        sigaddset(&signal_mask, SIGRTMIN + osk_n);
"""

SYNC_MAIN_HANDLER = r"""            else if ((int)si.ssi_signo > SIGRTMIN &&
                     (int)si.ssi_signo <= SIGRTMIN + OSK_MAX_LAYER_SIGS)
                osk_set_layer((int)si.ssi_signo - SIGRTMIN - 1); /* osk-patch: layout-sync */
"""

LAYOUT_CASE_RE = re.compile(
    r"(    case Layout:\n        // switch to the layout determined by the key\n(?:.*\n)*?)(        break;\n    case Compose:)")


def patch_sync(c, m):
    """keyboard.c + main.c: синхронизация слоя с системной раскладкой. Требует патч mod-lock+hold (osk_run)."""
    if M_SYNC in c and M_SYNC in m:
        return c, m, False
    if M_LOCK not in c:
        raise ValueError("«layout-sync»: сначала должен примениться патч mod-lock+hold")
    # --- keyboard.c ---
    c = sub1(c, r"(\nvoid\nkbd_press_key\(struct kbd \*kb, struct key \*k, uint32_t time\)\n\{)",
             lambda mm: "\n" + SYNC_KC_FUNC + mm.group(1), "kbd_press_key (layout-sync)", hint="kbd_press_key")
    if not LAYOUT_CASE_RE.search(c):
        raise ValueError("«layout-sync»: не нашёл обработчик case Layout в keyboard.c")
    c = LAYOUT_CASE_RE.sub(lambda mm: mm.group(1) +
                           "        osk_layout_changed(kb);        /* osk-patch: layout-sync */\n" + mm.group(2), c, count=1)
    # дочерние процессы не должны наследовать заблокированные сигналы
    c = sub1(c, r'(        setsid\(\);\n)(        execl\("/bin/sh")',
             lambda mm: mm.group(1) + "        { sigset_t osk_all; sigfillset(&osk_all); sigprocmask(SIG_UNBLOCK, &osk_all, NULL); }\n" + mm.group(2),
             "osk_run: сигналы потомка", hint="setsid")
    # --- main.c ---
    m = sub1(m, r'(#include "keyboard.h"\n)', lambda mm: mm.group(1) + "#include <signal.h>\n", "include signal.h", hint="keyboard.h")
    m = sub1(m, r"(\nvoid\nredimension_keyboard\(\)\n\{)", lambda mm: "\n" + SYNC_MAIN_PREF + mm.group(1),
             "redimension_keyboard (pref)", hint="redimension_keyboard")
    if not REDIM_RE.search(m) or not REDIM_TAIL_RE.search(m):
        raise ValueError("«layout-sync»: не нашёл redimension_keyboard в main.c")
    m = REDIM_RE.sub(lambda mm: REDIM_NEW, m, count=1)
    m = REDIM_TAIL_RE.sub(lambda mm: mm.group(1) + REDIM_TAIL_NEW, m, count=1)
    m = sub1(m, r"(\nvoid\ntoggle_visibility\(\)\n\{)", lambda mm: "\n" + SYNC_MAIN_FUNC + mm.group(1),
             "toggle_visibility", hint="toggle_visibility")
    m = sub1(m, r"(\nmain\(int argc, char \*\*argv\)\n\{\n)", lambda mm: mm.group(1) + SYNC_MAIN_EARLY, "main()", hint="main(")
    m = sub1(m, r"([ \t]*sigaddset\(&signal_mask, SIGRTMIN\);\n)", lambda mm: mm.group(1) + SYNC_MAIN_SIGNALS,
             "signal_mask", hint="signal_mask")
    m = sub1(m, r"([ \t]*else if \(si\.ssi_signo == SIGPIPE\))", lambda mm: SYNC_MAIN_HANDLER + mm.group(1),
             "SIGPIPE handler", hint="SIGPIPE")
    return c, m, True


# ---------------- плавающая клавиатура (osk-patch: float) ----------------
RELPTR_XML = r'''<?xml version="1.0" encoding="UTF-8"?>
<protocol name="relative_pointer_unstable_v1">

  <copyright>
    Copyright © 2014      Jonas Ådahl
    Copyright © 2015      Red Hat Inc.

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice (including the next
    paragraph) shall be included in all copies or substantial portions of the
    Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.
  </copyright>

  <description summary="protocol for relative pointer motion events">
    This protocol specifies a set of interfaces used for making clients able to
    receive relative pointer events not obstructed by barriers (such as the
    monitor edge or other pointer barriers).

    To start receiving relative pointer events, a client must first bind the
    global interface "wp_relative_pointer_manager" which, if a compositor
    supports relative pointer motion events, is exposed by the registry. After
    having created the relative pointer manager proxy object, the client uses
    it to create the actual relative pointer object using the
    "get_relative_pointer" request given a wl_pointer. The relative pointer
    motion events will then, when applicable, be transmitted via the proxy of
    the newly created relative pointer object. See the documentation of the
    relative pointer interface for more details.

    Warning! The protocol described in this file is experimental and backward
    incompatible changes may be made. Backward compatible changes may be added
    together with the corresponding interface version bump. Backward
    incompatible changes are done by bumping the version number in the protocol
    and interface names and resetting the interface version. Once the protocol
    is to be declared stable, the 'z' prefix and the version number in the
    protocol and interface names are removed and the interface version number is
    reset.
  </description>

  <interface name="zwp_relative_pointer_manager_v1" version="1">
    <description summary="get relative pointer objects">
      A global interface used for getting the relative pointer object for a
      given pointer.
    </description>

    <request name="destroy" type="destructor">
      <description summary="destroy the relative pointer manager object">
	Used by the client to notify the server that it will no longer use this
	relative pointer manager object.
      </description>
    </request>

    <request name="get_relative_pointer">
      <description summary="get a relative pointer object">
	Create a relative pointer interface given a wl_pointer object. See the
	wp_relative_pointer interface for more details.
      </description>
      <arg name="id" type="new_id" interface="zwp_relative_pointer_v1"/>
      <arg name="pointer" type="object" interface="wl_pointer"/>
    </request>
  </interface>

  <interface name="zwp_relative_pointer_v1" version="1">
    <description summary="relative pointer object">
      A wp_relative_pointer object is an extension to the wl_pointer interface
      used for emitting relative pointer events. It shares the same focus as
      wl_pointer objects of the same seat and will only emit events when it has
      focus.
    </description>

    <request name="destroy" type="destructor">
      <description summary="release the relative pointer object"/>
    </request>

    <event name="relative_motion">
      <description summary="relative pointer motion">
	Relative x/y pointer motion from the pointer of the seat associated with
	this object.

	A relative motion is in the same dimension as regular wl_pointer motion
	events, except they do not represent an absolute position. For example,
	moving a pointer from (x, y) to (x', y') would have the equivalent
	relative motion (x' - x, y' - y). If a pointer motion caused the
	absolute pointer position to be clipped by for example the edge of the
	monitor, the relative motion is unaffected by the clipping and will
	represent the unclipped motion.

	This event also contains non-accelerated motion deltas. The
	non-accelerated delta is, when applicable, the regular pointer motion
	delta as it was before having applied motion acceleration and other
	transformations such as normalization.

	Note that the non-accelerated delta does not represent 'raw' events as
	they were read from some device. Pointer motion acceleration is device-
	and configuration-specific and non-accelerated deltas and accelerated
	deltas may have the same value on some devices.

	Relative motions are not coupled to wl_pointer.motion events, and can be
	sent in combination with such events, but also independently. There may
	also be scenarios where wl_pointer.motion is sent, but there is no
	relative motion. The order of an absolute and relative motion event
	originating from the same physical motion is not guaranteed.

	If the client needs button events or focus state, it can receive them
	from a wl_pointer object of the same seat that the wp_relative_pointer
	object is associated with.
      </description>
      <arg name="utime_hi" type="uint"
	   summary="high 32 bits of a 64 bit timestamp with microsecond granularity"/>
      <arg name="utime_lo" type="uint"
	   summary="low 32 bits of a 64 bit timestamp with microsecond granularity"/>
      <arg name="dx" type="fixed"
	   summary="the x component of the motion vector"/>
      <arg name="dy" type="fixed"
	   summary="the y component of the motion vector"/>
      <arg name="dx_unaccel" type="fixed"
	   summary="the x component of the unaccelerated motion vector"/>
      <arg name="dy_unaccel" type="fixed"
	   summary="the y component of the unaccelerated motion vector"/>
    </event>
  </interface>

</protocol>
'''

M_FLOAT = "/* osk-patch: float */"
M_TAP = "/* osk-patch: tap-cmd */"

FLOAT_MAIN_FUNCS = M_FLOAT + r'''
/* Плавающий режим: клавиатура - небольшая поверхность в углу (якорь top+left, поля = позиция), пропорционально
   уменьшенная (fl_scale); круглая кнопка в левом верхнем углу: клик - свернуть в саму кнопку / развернуть,
   перетаскивание - двигать. Позицию и состояние сообщаем команде $OSK_FLOAT_CMD (<что> <a> <b>).
   Перетаскивание мышью идёт по относительным смещениям (zwp_relative_pointer): координаты внутри поверхности
   при движении самой поверхности у композитора «плывут». Без этого протокола (и для касаний) поверхность
   переносится один раз, при отпускании. */
static int fl_on = 0, fl_collapsed = 0, fl_x = 24, fl_y = 24;
static double fl_scale = 0.5;
static uint32_t fl_btn = 40;
static int fl_drag = 0, fl_moved = 0, fl_lx0 = 0, fl_ly0 = 0, fl_wait = 0;
static int fl_sx = 0, fl_sy = 0, fl_lastx = 0, fl_lasty = 0, fl_relmode = 0;
static double fl_accx = 0, fl_accy = 0;
static struct zwp_relative_pointer_manager_v1 *fl_rel_mgr;
static struct zwp_relative_pointer_v1 *fl_rel_ptr;

static void
osk_fl_cmd(const char *what, int a, int b)
{
    const char *base = getenv("OSK_FLOAT_CMD");
    char cmd[1024];
    if (!base || !*base)
        return;
    snprintf(cmd, sizeof cmd, "%s %s %d %d", base, what, a, b);
    if (fork() == 0) {
        sigset_t all;
        setsid();
        sigfillset(&all);
        sigprocmask(SIG_UNBLOCK, &all, NULL);
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
}

static void
osk_fl_clamp(int *x, int *y)
{
    int mx = (int)available_width - (int)keyboard.w;
    int my = (int)available_height - (int)keyboard.h;
    if (mx < 0) mx = 0;
    if (my < 0) my = 0;
    if (*x > mx) *x = mx;
    if (*y > my) *y = my;
    if (*x < 0) *x = 0;
    if (*y < 0) *y = 0;
}

static void osk_fl_flush(int force);

static void
osk_fl_sync_done(void *data, struct wl_callback *cb, uint32_t t)
{
    fl_wait = 0;
    wl_callback_destroy(cb);
    if (fl_drag && fl_relmode)
        osk_fl_flush(0);
}

static const struct wl_callback_listener osk_fl_sync_listener = {
    .done = osk_fl_sync_done,
};

/* применить накопленное смещение: поля поверхности = старт + смещение (с ограничением по экрану) */
static void
osk_fl_flush(int force)
{
    int nx = fl_sx + (int)(fl_accx + (fl_accx < 0 ? -0.5 : 0.5));
    int ny = fl_sy + (int)(fl_accy + (fl_accy < 0 ? -0.5 : 0.5));
    if (!layer_surface || (fl_wait && !force))
        return;
    osk_fl_clamp(&nx, &ny);
    fl_accx = nx - fl_sx;                 /* у края накопление останавливается: обратное движение сразу двигает окно */
    fl_accy = ny - fl_sy;
    if (nx == fl_x && ny == fl_y)
        return;
    fl_x = nx;
    fl_y = ny;
    zwlr_layer_surface_v1_set_margin(layer_surface, fl_y, 0, 0, fl_x);
    wl_surface_commit(draw_surf.surf);
    if (!force) {                         /* пока композитор не применил позицию, новую не шлём */
        fl_wait = 1;
        wl_callback_add_listener(wl_display_sync(display), &osk_fl_sync_listener, NULL);
    }
}

static void
osk_fl_rel_motion(void *data, struct zwp_relative_pointer_v1 *rp, uint32_t hi, uint32_t lo,
                  wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t dxu, wl_fixed_t dyu)
{
    if (!fl_drag || !fl_relmode)
        return;
    fl_accx += wl_fixed_to_double(dx);
    fl_accy += wl_fixed_to_double(dy);
    if (!fl_moved) {
        if (fl_accx * fl_accx + fl_accy * fl_accy < 64.0)
            return;
        fl_moved = 1;
    }
    osk_fl_flush(0);
}

static const struct zwp_relative_pointer_v1_listener osk_fl_rel_listener = {
    .relative_motion = osk_fl_rel_motion,
};

static int
osk_fl_press(int x, int y, int via_pointer)
{
    struct key *k;
    if (!fl_on)
        return 0;
    if (!fl_collapsed) {
        k = kbd_get_key(&keyboard, x, y);
        if (!k || k->type != FloatBtn)
            return 0;
    }
    if (via_pointer && fl_rel_mgr && pointer && !fl_rel_ptr) {
        fl_rel_ptr = zwp_relative_pointer_manager_v1_get_relative_pointer(fl_rel_mgr, pointer);
        zwp_relative_pointer_v1_add_listener(fl_rel_ptr, &osk_fl_rel_listener, NULL);
    }
    fl_relmode = via_pointer && fl_rel_ptr != NULL;
    fl_drag = 1;
    fl_moved = 0;
    fl_lx0 = fl_lastx = x;
    fl_ly0 = fl_lasty = y;
    fl_sx = fl_x;
    fl_sy = fl_y;
    fl_accx = fl_accy = 0;
    fl_wait = 0;
    return 1;
}

/* координаты внутри поверхности: нужны для касаний и когда нет relative-pointer (поверхность не двигается до отпускания) */
static void
osk_fl_motion(int x, int y)
{
    if (!fl_drag || fl_relmode)
        return;
    fl_lastx = x;
    fl_lasty = y;
    if (!fl_moved && abs(x - fl_lx0) + abs(y - fl_ly0) >= 8)
        fl_moved = 1;
}

static void
osk_fl_release(void)
{
    if (!fl_drag)
        return;
    fl_drag = 0;
    if (fl_moved) {
        if (!fl_relmode) {
            fl_accx = fl_lastx - fl_lx0;
            fl_accy = fl_lasty - fl_ly0;
        }
        osk_fl_flush(1);
        osk_fl_cmd("pos", fl_x, fl_y);
    } else {
        fl_collapsed = !fl_collapsed;
        osk_fl_cmd(fl_collapsed ? "collapsed" : "expanded", fl_x, fl_y);
        hide();
        show();
    }
}

'''

FLOAT_REDIM = M_FLOAT + r'''
    keyboard.osk_fl = 0;
    if (fl_on) {
        if (fl_collapsed) {
            keyboard.w = height = fl_btn;
            keyboard.osk_fl = 2;
        } else {
            uint32_t fh = (uint32_t)((double)height * fl_scale);
            keyboard.w = (uint32_t)((double)available_width * fl_scale);
            height = fh < 90 ? 90 : fh;
            keyboard.osk_fl = 1;
        }
    }
'''

FLOAT_SHOW = M_FLOAT + r'''
    if (fl_on) {
        osk_fl_clamp(&fl_x, &fl_y);
        zwlr_layer_surface_v1_set_anchor(layer_surface,
                                         ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT);
        zwlr_layer_surface_v1_set_size(layer_surface, keyboard.w, height);
        zwlr_layer_surface_v1_set_margin(layer_surface, fl_y, 0, 0, fl_x);
        zwlr_layer_surface_v1_set_exclusive_zone(layer_surface, 0);
    }
'''

FLOAT_ARGS = M_FLOAT + r'''
        } else if (!strcmp(argv[i], "--float")) {
            /* --float X,Y[,свёрнута]: плавающий режим, позиция (поля от левого верхнего угла) и состояние */
            int fx = 24, fy = 24, fc = 0;
            if (i >= argc - 1) {
                usage(argv[0]);
                exit(1);
            }
            sscanf(argv[++i], "%d,%d,%d", &fx, &fy, &fc);
            fl_on = 1;
            fl_x = fx < 0 ? 0 : fx;
            fl_y = fy < 0 ? 0 : fy;
            fl_collapsed = fc ? 1 : 0;
        } else if (!strcmp(argv[i], "--float-scale")) {
            if (i >= argc - 1) {
                usage(argv[0]);
                exit(1);
            }
            fl_scale = atof(argv[++i]);
            if (fl_scale < 0.2) fl_scale = 0.2;
            if (fl_scale > 1.0) fl_scale = 1.0;
        } else if (!strcmp(argv[i], "--float-button")) {
            if (i >= argc - 1) {
                usage(argv[0]);
                exit(1);
            }
            fl_btn = (uint32_t)atoi(argv[++i]);
            if (fl_btn < 24) fl_btn = 24;
            if (fl_btn > 120) fl_btn = 120;
'''

FLOAT_DRAW_FUNC = M_FLOAT + r'''
static void
osk_draw_fbtn(struct kbd *kb, uint32_t x, uint32_t y, uint32_t w, uint32_t h)
{
    uint32_t d = w < h ? w : h, d2;
    Color amber = {.bgra = {0x20, 0xA0, 0xE0, 0xFF}};
    Color dark = {.bgra = {0x28, 0x28, 0x28, 0xFF}};
    if (d > 8)
        d -= 4;
    x += (w - d) / 2;
    y += (h - d) / 2;
    drw_fill_rectangle(kb->surf, amber, x, y, d, d, (int)(d / 2));
    d2 = d / 3;
    if (d2 > 1)
        drw_fill_rectangle(kb->surf, dark, x + (d - d2) / 2, y + (d - d2) / 2, d2, d2, (int)(d2 / 2));
}

'''


def patch_float(c, h, m):
    """Плавающий режим: main.c, keyboard.c, keyboard.h. Требует патчи mod-lock+hold и layout-sync."""
    if M_FLOAT in m and M_FLOAT in c:
        return c, h, m, False
    if M_SYNC not in m:
        raise ValueError("«float»: сначала должен примениться патч layout-sync")
    px = Path("proto/relative-pointer-unstable-v1.xml")      # протокол относительного указателя (для перетаскивания)
    if not px.exists():
        px.write_text(RELPTR_XML, encoding="utf-8")
    # --- keyboard.h ---
    h = sub1(h, r"(\n\tEndRow,)", lambda mm: "\n\tFloatBtn,  // osk: круглая кнопка плавающего режима" + mm.group(1),
             "enum key_type", hint="EndRow")
    h = sub1(h, r"(struct kbd \{\n)", lambda mm: mm.group(1) +
             "\tint osk_fl;  /* osk: 0 - обычный режим, 1 - плавающая, 2 - свёрнута в кнопку */\n",
             "struct kbd (float)", hint="struct kbd")
    # --- keyboard.c ---
    c = sub1(c, r"(\nvoid\nkbd_draw_key\(struct kbd \*kb, struct key \*k, enum key_draw_type type\)\n\{)",
             lambda mm: "\n" + FLOAT_DRAW_FUNC + mm.group(1), "kbd_draw_key", hint="kbd_draw_key")
    c = sub1(c, r"(    struct clr_scheme \*scheme = &kb->schemes\[k->scheme\];\n)",
             lambda mm: mm.group(1) + "    if (k->type == FloatBtn) {          " + M_FLOAT + "\n"
             "        osk_draw_fbtn(kb, k->x, k->y, k->w, k->h);\n        return;\n    }\n",
             "scheme в kbd_draw_key", hint="struct clr_scheme *scheme")
    c = sub1(c, r"(    struct drwsurf \*d = kb->surf;\n    struct key \*next_key = kb->layout->keys;\n)",
             lambda mm: mm.group(1) + "    if (kb->osk_fl == 2) {              " + M_FLOAT + "\n"
             "        drw_do_clear(d, 0, 0, kb->w, kb->h);\n        osk_draw_fbtn(kb, 0, 0, kb->w, kb->h);\n        return;\n    }\n",
             "kbd_draw_layout", hint="kbd_draw_layout")
    c = sub1(c, r"(kbd_press_key\(struct kbd \*kb, struct key \*k, uint32_t time\)\n\{\n)",
             lambda mm: mm.group(1) + "    if (k->type == FloatBtn)\n        return;\n", "kbd_press_key (float)", hint="kbd_press_key")
    # --- main.c ---
    m = sub1(m, r"(#include \"proto/input-method-unstable-v2-protocol.h\"\n)",
             lambda mm: mm.group(1) + "#include \"proto/relative-pointer-unstable-v1-client-protocol.h\"   " + M_FLOAT + "\n",
             "include relative-pointer", hint="input-method-unstable-v2-protocol.h")
    m = sub1(m, r"(    \} else if \(im_auto && strcmp\(interface, zwp_input_method_manager_v2_interface\.name\) == 0\) \{)",
             lambda mm: "    } else if (strcmp(interface, zwp_relative_pointer_manager_v1_interface.name) == 0) {   " + M_FLOAT + "\n"
             "        fl_rel_mgr = wl_registry_bind(registry, name, &zwp_relative_pointer_manager_v1_interface, 1);\n" + mm.group(1),
             "handle_global", hint="im_auto")
    m = sub1(m, r"(\nvoid\nwl_touch_down\()", lambda mm: "\n" + FLOAT_MAIN_FUNCS + mm.group(1), "wl_touch_down", hint="wl_touch_down")
    m = sub1(m, r"(    touch_x = wl_fixed_to_int\(x\);\n    touch_y = wl_fixed_to_int\(y\);\n\n    kbd_unpress_key\(&keyboard, time\);\n)",
             lambda mm: "    touch_x = wl_fixed_to_int(x);\n    touch_y = wl_fixed_to_int(y);\n"
             "    if (osk_fl_press((int)touch_x, (int)touch_y, 0))          " + M_FLOAT + "\n        return;\n\n"
             "    kbd_unpress_key(&keyboard, time);\n", "touch_down", hint="touch_x")
    m = sub1(m, r"(    kbd_release_key\(&keyboard, time\);\n\}\n\nvoid\nwl_touch_motion)",
             lambda mm: "    if (fl_drag) {                          " + M_FLOAT + "\n        osk_fl_release();\n        return;\n    }\n" + mm.group(1),
             "touch_up", hint="wl_touch_motion")
    m = sub1(m, r"(    kbd_motion_key\(&keyboard, time, touch_x, touch_y\);\n)",
             lambda mm: "    if (fl_drag) {                          " + M_FLOAT + "\n        osk_fl_motion((int)touch_x, (int)touch_y);\n        return;\n    }\n" + mm.group(1),
             "touch_motion", hint="kbd_motion_key")
    m = sub1(m, r"(    cur_y = wl_fixed_to_int\(surface_y\);\n\n)(    if \(cur_press\) \{\n        kbd_motion_key)",
             lambda mm: mm.group(1) + "    if (fl_drag) {                          " + M_FLOAT + "\n        osk_fl_motion(cur_x, cur_y);\n        return;\n    }\n" + mm.group(2),
             "pointer_motion", hint="cur_press")
    m = sub1(m, r"(    cur_press = state == WL_POINTER_BUTTON_STATE_PRESSED;\n)",
             lambda mm: mm.group(1) + "    if (fl_on) {                            " + M_FLOAT + "\n"
             "        if (cur_press && cur_x >= 0 && cur_y >= 0 && osk_fl_press(cur_x, cur_y, 1))\n            return;\n"
             "        if (!cur_press && fl_drag) {\n            osk_fl_release();\n            return;\n        }\n    }\n",
             "pointer_button", hint="cur_press =")
    m = sub1(m, r"(    keyboard\.h = height;\n)", lambda mm: FLOAT_REDIM + mm.group(1), "redimension", hint="keyboard.h = height")
    m = sub1(m, r"(    zwlr_layer_surface_v1_set_anchor\(layer_surface, anchor\);\n    if \(keyboard\.exclusive\) \{\n"
                r"        zwlr_layer_surface_v1_set_exclusive_zone\(layer_surface, height\);\n    \}\n)",
             lambda mm: mm.group(1) + FLOAT_SHOW, "show()", hint="set_exclusive_zone")
    m = sub1(m, r"(        \} else \{\n            fprintf\(stderr, \"Invalid argument)",
             lambda mm: FLOAT_ARGS + mm.group(1), "разбор аргументов", hint="Invalid argument")
    return c, h, m, True


def patch_tap(c, h):
    """Клавиша с командой на короткое нажатие (tap_cmd): Menu открывает наше меню, удержание - настоящий Menu."""
    if M_TAP in c:
        return c, h, False
    if M_LOCK not in c:
        raise ValueError("«tap-cmd»: сначала должен примениться патч mod-lock+hold")
    h = sub1(h, r"([ \t]+)(const char \*hold_cmd;[^\n]*\n)",
             lambda mm: mm.group(1) + mm.group(2) + mm.group(1) + "const char *tap_cmd;     /* osk: команда оболочки на короткое нажатие */\n",
             "struct key (tap_cmd)", hint="hold_cmd")
    c = sub1(c, r"if \(k->hold_code \|\| k->hold_cmd\) \{",
             "if (k->hold_code || k->hold_cmd || k->tap_cmd) {", "HOLD_HOOK", hint="hold_cmd")
    c = sub1(c, r"(    else if \(held && k->hold_code\)\n        osk_send_combo\(kb, time, k->hold_mod, k->hold_code\);\n)",
             lambda mm: mm.group(1) + "    else if (!held && k->tap_cmd)               " + M_TAP + "\n        osk_run(k->tap_cmd);\n",
             "osk_fire_hold", hint="osk_send_combo")
    return c, h, True

def main():
    if not KC.exists() or not KH.exists() or not KM.exists():
        sys.exit("patch_keyboard: нет keyboard.c/keyboard.h/main.c, запускай из каталога wvkbd")
    c, h, m = KC.read_text(encoding="utf-8"), KH.read_text(encoding="utf-8"), KM.read_text(encoding="utf-8")
    try:
        c, ch_caps = patch_caps(c)
        c, h, ch_lock = patch_lock(c, h)
        c, m, ch_sync = patch_sync(c, m)
        c, h, ch_tap = patch_tap(c, h)
        c, h, m, ch_float = patch_float(c, h, m)
    except ValueError as e:
        sys.exit(f"patch_keyboard: {e}\nФайлы не изменены. Версия wvkbd отличается - пришли соответствующий кусок кода.")
    if not (ch_caps or ch_lock or ch_sync or ch_tap or ch_float):
        print("patch_keyboard: все правки уже применены")
        return
    for f in (KC, KH, KM):
        if not Path(str(f) + ".orig").exists():
            shutil.copyfile(f, str(f) + ".orig")
    KC.write_text(c, encoding="utf-8")
    KH.write_text(h, encoding="utf-8")
    KM.write_text(m, encoding="utf-8")
    done = [n for n, ok in (("caps", ch_caps), ("mod-lock+hold", ch_lock), ("layout-sync", ch_sync), ("tap-cmd", ch_tap), ("float", ch_float)) if ok]
    print("patch_keyboard: применено: " + ", ".join(done))


if __name__ == "__main__":
    main()

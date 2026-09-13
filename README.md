# wpe-studio

A Wallpaper Engine front end that renders on Linux, driving
`linux-wallpaperengine` against the wallpapers Steam already downloaded.

```
wpe-studio                open the app
wpe-apply <id>            apply one, no UI
wpe-apply --restore       re-apply what each monitor last had (runs at login)
wpe-apply --list          what is installed
wpe-apply --props <id>    a wallpaper's settings and their current values
wpe-apply --set <id> k=v  change a setting and re-apply if it is on screen
```

The UI is also a plain web app on `http://127.0.0.1:8014`, so it works from a
browser and can be put behind the remote dashboard.

## What it runs on

Built and used on **Linux Mint 22 / Cinnamon / X11**. It is not tied to Mint,
but it is tied to X11, and the desktop-integration parts differ per desktop:

| | Cinnamon | GNOME / Budgie | MATE | Xfce | KDE |
|---|---|---|---|---|---|
| render the wallpaper | yes | yes | yes | yes | yes |
| lock screen background | yes | likely | yes | yes | no |
| desktop-icons toggle | yes | yes | yes | manual | manual |
| idle timeout / lock settings | yes | manual | manual | manual | manual |

Rendering is X11-only. The whole approach is "take an ordinary window and demote
it to the desktop layer", driven through `xrandr`, `wmctrl` and `xprop`.
**On Wayland nothing will appear** — `linux-wallpaperengine` itself supports
`wlr-layer-shell`, but this wrapper does not drive it yet.

Everything environment-specific lives in `wpestudio/desktop.py`, and anything
unsupported is reported in the UI rather than silently doing nothing. Adding a
desktop is usually a row in the table there.

Requirements: `linux-wallpaperengine`, Python 3.10+, `xrandr`/`wmctrl`/`xprop`/
`xdotool`, ImageMagick (for the lock screen still), and Wallpaper Engine owned
on Steam. GTK3 + WebKit2GTK for the app window; without them it opens in your
browser instead.

## Why it exists

Wallpaper Engine's own UI is a Windows app. Under Proton it launches, browses
and even lets you press Apply — and nothing happens, because the renderer
cannot touch the Linux desktop. `linux-wallpaperengine` is the renderer;
this is the front end for it.

Everything visual is lifted from the installed app rather than reinvented:
the palettes come out of `ui/dist/styles/skin*.css`, the icons are the same
Font Awesome 6 Pro webfonts it ships, and labels resolve through its own
`locale/ui_en-us.json`, so a property called `ui_browse_properties_scheme_color`
reads "Scheme color" here too.

## What it does

**Library** — every wallpaper under
`steamapps/workshop/content/431960`, plus the defaults that ship with the app
and anything in `projects/myprojects`. Filter by type, source, content rating
and tag; search; favourite; sort.

**Properties** — each wallpaper's `general.properties` becomes real controls:
sliders with the author's min/max/step, colour pickers (stored as the
`"r g b"` floats the format uses), combos, text inputs, toggles. The
`condition` expressions are evaluated so options hide when their parent is
off, exactly like the real app. Changes are debounced and re-applied live,
saved per wallpaper id, and can be stored as named presets.

**Presets** — a Workshop "preset" item is not a wallpaper. It has no `type`
and no `file`; it carries a `dependency` (the wallpaper it customises) and a
`preset` blob of values. Running one directly does nothing, which is why five
of them looked broken. They now resolve to their base wallpaper, with their
values applied and any relative asset path (`files/whatever.png`) rewritten to
an absolute one, because the engine would otherwise resolve it against the
*base* wallpaper's folder where the file does not exist.

**Screensaver / lock screen** — see below.

**Steam** — the Workshop tab is the real steamcommunity.com page in an
embedded WebView with its own persistent cookie jar, so you stay signed in and
Subscribe works in place. Steam's Play button can be pointed at this app.

**Playlists** — rotate wallpapers on a timer, in order or shuffled.

## The three things that make it work on this box

Each of these was a silent failure, so they are worth keeping written down.

**1. The desktop window.** `--screen-root` renders *underneath* Cinnamon's own
desktop window: the process looks healthy and the screen stays black. Instead
the engine gets an ordinary window at the monitor's geometry which is then
demoted by hand — `_NET_WM_WINDOW_TYPE_DESKTOP`, then `remove,above` and
`remove,fullscreen` **before** `add,below`, or BELOW never sticks. Desktop
icons must also be off, otherwise `nemo-desktop` owns the root window and
covers everything; the UI checks and says so.

**2. CEF's resources.** `libcef.so` looks for `icudtl.dat` and the `.pak`
files next to itself in `cef/*/Release/`, and the CEF distribution puts them
in `Resources/`. Without them CEF blocks forever with no error — every `web`
wallpaper appeared to start and then hang. `Release/` now symlinks them.

**3. Web wallpapers: three bugs deep.** First, inside CEF:

    ImmediateCrash() at base/immediate_crash.h:186
    close() at base/files/scoped_file_linux.cc:110
    AdjustLinuxOOMScore() at chrome/app/chrome_main_delegate.cc:312
    CefInitialize() at cef/libcef/browser/context.cc:331

Chromium overrides `close()` and resolves the real one with
`dlsym(RTLD_NEXT, "close")`. libcef was pulled in only transitively, so it sat
*after* libc in the link map, `RTLD_NEXT` found nothing, and
`CHECK(g_libc_close) << "close symbol missing"` fired. Fixed by linking libcef
into the executable ahead of libc (`-Wl,--no-as-needed`, since main.cpp
references no libcef symbol and the linker drops it otherwise).

Then CEF's child processes died, because `CefExecuteProcess` was called from
deep inside `WallpaperApplication`'s constructor: a child first ran the whole
wallpaper startup and fed Chromium's switches to the wallpaper argument parser.
`src/main.cpp` now calls it first thing when it sees `--type=`, using
`EarlySubprocessApp`, which reads the custom scheme names out of
`WPE_CEF_SCHEMES` (the parent publishes them before `CefInitialize`, since a
child cannot rebuild the list without the loaded backgrounds).

With the process tree finally starting cleanly, `OnPaint` was firing at the
right size every frame — and the screen was still black. Two reasons, both in
`RenderHandler`:

* it bound `getWallpaperFramebuffer()` with `glBindTexture`. GL names live in
  separate namespaces per object type, so the framebuffer's id is not the
  texture's id; CEF's pixels were being uploaded into an unrelated texture.
  `getWallpaperTexture()` already existed.
* `CWeb::renderFrame` binds the scene FBO *before* pumping CEF's message loop,
  so `OnPaint` ran with the destination texture attached to the bound
  framebuffer. Writing to an attachment of the active framebuffer is a feedback
  loop and the upload is discarded. It now detaches for the upload and restores
  the binding, and uses `glTexSubImage2D` unless the size changed rather than
  reallocating the FBO's colour attachment every frame.

Web wallpapers render. Set `WPE_CEF_DEBUG=1` to log `OnPaint` and the registered
custom schemes.

Two scene wallpapers still fail, in the engine's own scene loader
("Projection must have a width", an nlohmann `type_error`). Those are marked
Unsupported in the UI, with the recorded error on the tile.

## Lock screen

**Mint's own lock screen does the job.** cinnamon-screensaver already draws the
pill with the avatar and the password box, and it already does PAM correctly.

An earlier version of this replaced it with xsecurelock plus a custom saver, to
get a *live* wallpaper behind the prompt. That was a mistake and is gone:

* Cinnamon's `custom-screensaver-command` makes cinnamon-screensaver **exit**
  and hands over drawing, input grabbing and authentication. That is a lot of
  responsibility to take on for a background.
* The custom panel drew its own password field while xsecurelock drew the real
  one, so the screen showed two.
* The overlay could not be composited anyway. It needs an ARGB (32-bit) window
  to be transparent, the saver window is 24-bit, and `XReparentWindow` across
  depths fails — silently, through xdotool. The overlay stayed a top-level
  window floating over everything.

What is left is small and reliable: one frame is rendered from the chosen
wallpaper and set as `org.cinnamon.desktop.background picture-uri`, which is
the key cinnamon-screensaver reads for its background
(`/usr/share/cinnamon-screensaver/util/settings.py:9`). The stock pill, over
the wallpaper's imagery. Idle timeout is `org.cinnamon.desktop.session
idle-delay`; the password requirement is `lock-enabled`. Nothing about the
login flow is ours.

Side effect, and a welcome one: that key is also the desktop's static
background, so stopping the live wallpaper falls back to a still of it rather
than to black.

**The still frame is rendered behind the desktop, not over it.** The engine is
launched, demoted to `_NET_WM_WINDOW_TYPE_DESKTOP`, and only then does the
grab run — and it is killed on every exit path including the timeout. The
first version skipped the demotion and a full-screen render sat on top of the
desktop for the length of the grab, which locked the operator out of his own
machine until it was killed by hand.

The greeter is the one thing Cinnamon cannot cover, because LightDM runs before
the session exists. The same still can be installed as its background through
one root step, popped as a terminal
(`ask-sudo --title ... bash <script>` — the title is a flag; passing it
positionally makes it the command and fails with a bare "not found").

## Steam's Play button

`Play` on app 431960 normally starts the Windows build under Proton. The UI can
rewrite the per-user launch option in `localconfig.vdf` to:

    /usr/bin/flatpak-spawn --host /ai/wpe-studio/bin/wpe-studio --from-steam %command%

`flatpak-spawn --host` because Steam here is a flatpak and this app lives
outside the sandbox; the two `flatpak override --user` grants it needs are
applied at the same time and need no root.

**Steam rewrites `localconfig.vdf` when it exits**, so an edit made while Steam
is running is thrown away. The toggle closes Steam, writes, and reopens it, and
refuses to pretend otherwise. The file is backed up to
`localconfig.vdf.wpe-studio.bak` on first write, the edit is a targeted splice
rather than a parse-and-regenerate of the whole client config, and it is
rejected if the brace balance changes.

## The HTTP API

On loopback there is no auth, because anything that can reach it can already run
`wpe-apply`. Bind it anywhere else (`WPE_BIND`) and a token is generated into
`~/.config/wpe-studio/api-token` and required on every request, as
`Authorization: Bearer <token>` or `?t=<token>`; the server prints a ready-made
URL on startup and the page carries the token through its own calls. That is not
a substitute for putting it behind Tailscale, but it is the difference between
one step and none.

## Layout

```
wpestudio/paths.py     every path, all overridable by env (Docker uses that)
wpestudio/state.py     one JSON file, atomic writes
wpestudio/library.py   scanning, project.json, property schemas, presets
wpestudio/engine.py    argv, process control, the window demotion
wpestudio/steamio.py   Workshop browse, launch-option hook
wpestudio/lockscreen.py  cinnamon-screensaver background, greeter still
wpestudio/lockscreen.py  lock screen background, greeter still
wpestudio/desktop.py     desktop/session detection, per-DE integration
wpestudio/server.py    stdlib HTTP, JSON API, static files
web/                   the UI
bin/                   wpe-studio, wpe-apply
docker/                Dockerfile + compose.yml
```

State lives in `~/.config/wpe-studio/state.json`, logs in
`~/.cache/wpe-studio/logs/`.

## Install

```
ln -sf /ai/wpe-studio/bin/wpe-studio /ai/bin/wpe-studio
ln -sf /ai/wpe-studio/bin/wpe-apply  /ai/bin/wpe-apply
cp /ai/wpe-studio/wpe-studio.desktop ~/.local/share/applications/
cp /ai/wpe-studio/systemd/wpe-studio.service ~/.config/systemd/user/
systemctl --user enable --now wpe-studio
```

Autostart of the wallpaper itself is a toggle in Settings; it writes
`~/.config/autostart/wpe-studio.desktop` and removes the old `wpe.desktop`
so the two do not fight.

## Docker

```
docker compose -f /ai/wpe-studio/docker/compose.yml up -d
```

Host networking, the X socket, the nvidia runtime and the host's engine build
mounted at the same path its RPATH expects. It renders on the host display and
serves the same UI.

It deliberately cannot do the host-only things — the Steam launch option,
installing xsecurelock, the desktop-icons toggle, the autostart entry — because
`gsettings`, `systemctl --user` and `xdg-open` would act on the container. The
server sets `docker: true` in `/api/bootstrap` and the UI greys those out
rather than appearing to work.

## Predecessors

`~/.local/bin/wpe` and `wpe-picker` from an earlier session still work and are
left alone. This supersedes both; `wpe-apply --stop` also stops anything they
started, and enabling autostart here removes theirs.

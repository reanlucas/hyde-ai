<div align="center">

# hyde-ai

**An LLM chat sidebar for Hyprland.**

GTK4 with layer-shell, coloured by
[HyDE](https://github.com/HyDE-Project/HyDE)'s wallbash.

Claude · Gemini · ChatGPT · local Ollama — all streaming.

</div>

> [!WARNING]
> **Beta. Not recommended for use.**
>
> Written for one specific setup and still unstable: the interface has rough
> edges, the API changes without notice and there are untested paths. If you
> try it, expect breakage — and don't rely on it for anything that matters.
>
> **Agent mode** has only been tested with `qwen3.5:9b`. Any other model is
> unknown ground: it runs commands on your machine, and a model that uses the
> tools differently may behave in ways I have not seen.

---

<div align="center">
  <img src="docs/sidebar.gif" width="860" alt="The sidebar slides in from the right edge over the desktop, slides out, reopens docked to the left edge, then returns to the right">
  <p><em>A real sidebar: layer-shell, sliding over the desktop without resizing
  anything. <code>/side left</code> moves it to the other edge.</em></p>
</div>

<div align="center">
  <img src="docs/ssh.gif" width="400" alt="Asking how to configure a GitHub SSH connection on Arch Linux; the answer streams in with numbered steps and syntax-highlighted shell blocks">
  &nbsp;&nbsp;
  <img src="docs/painel.png" width="400" alt="The finished answer: numbered steps with ssh-keygen, chmod and ssh -T commands in highlighted code blocks, with the reasoning collapsed at the bottom">
  <p><em>A practical question, answered by Qwen3.5 9B running locally.
  Syntax-highlighted, line-numbered code blocks. Sped up 3.5&times;.</em></p>
</div>

<div align="center">
  <img src="docs/conversa.gif" width="400" alt="A maths question answered live: five display formulas typeset one after another as the answer streams in">
  <p><em>And a calculus one, with the formulas typeset as they arrive.</em></p>
</div>

---

## Typeset mathematics

Formulas go through matplotlib's `mathtext`, a complete TeX parser, and come
out as **SVG rasterised at the screen's real density** — sharp at any scale.

<div align="center">
  <img src="docs/matematica.png" width="400" alt="Rendered formulas: a Gaussian integral with limits, a stacked fraction, a nested square root and a boxed result, all typeset in the STIX font">
  <p><em>Stacked fractions, integrals with limits, radicands under the bar.
  STIX &mdash; the font used in scientific publishing.</em></p>
</div>

Long equations are broken at the relation signs, like LaTeX's `align`
environment — without that they turned into a wide strip with a horizontal
scrollbar.

Clicking a formula copies the LaTeX; double-clicking reveals the source as
selectable text.

---

## Installation

### 1. Prerequisites

Arch Linux with Hyprland and [HyDE](https://github.com/HyDE-Project/HyDE).

### 2. Clone and install

```bash
git clone https://github.com/reanlucas/hyde-ai.git
cd hyde-ai
./install.sh
```

The installer resolves the dependencies, copies the files, renders the theme
colours and runs a diagnostic at the end. It is idempotent.

### 3. Configure the providers

```bash
hyde-ai --setup
```

Asks for the API keys and the default model. The config is written to
`~/.config/hyde-ai/config.json` with mode `0600`.

**Ollama needs no key.** If the service is already running, its models show up
on their own:

```bash
sudo pacman -S ollama-rocm      # AMD; use "ollama" for CPU/NVIDIA
sudo systemctl enable --now ollama
ollama pull qwen3.5:9b
```

### 4. Open it

```bash
hyde-ai --toggle
```

### 5. Keybind and autostart (optional)

In `~/.config/hypr/hyprland.lua`:

```lua
hl.bind("SUPER + I", hl.dsp.exec_cmd("$HOME/.local/bin/hyde-ai --toggle"), {
    description = "[Launcher|Apps] AI sidebar",
})

hl.on("hyprland.start", function()
    hl.exec_cmd("$HOME/.local/bin/hyde-ai --daemon")
end)
```

---

## Using it

`Enter` sends · `Shift+Enter` newline · `Esc` closes

Type **`/`** in the input to open the command palette, which filters as you
write.

| Command | What it does |
|---|---|
| `/clear` | new conversation |
| `/history` | list the saved conversations |
| `/provider` `/model` | switch provider or model |
| `/key <provider> <value>` | store an API key |
| `/speed` | average tokens/s per model |
| `/refresh` | re-probe providers and models |
| `/think auto\|on\|off` | reasoning draft |
| `/agent on\|off` | let the model run commands here |
| `/side left\|right` · `/width 35` | panel geometry |

From the shell, `hyde-ai --ask "your question"` opens the panel and sends the
question straight through — handy on a keybind or from a script.

---

## Speed

Two measurements, deliberately distinct:

- **Next to the model** — a rolling average of the last 60 runs: *how it has
  been performing*
- **In each reply's header** — time to first token, generation time and the
  speed of **that** reply

```
qwen3.5:9b  ·  0.8s thinking  ·  14.7s  ·  61 tok/s
```

The time is split on purpose: *thinking* covers submission to first token
(loading, prompt eval). The speed is computed over generation alone —
including the wait would drag the number down for the wrong reason.

The metrics are stored with the message, so they travel with the conversation.

---

## Reasoning models

Qwen3, the o-series and Claude with *thinking* produce a draft before the
answer.

**Off by default** — the draft multiplies response time by five or more, and
rarely earns that in a sidebar. The button next to send turns it on per
question; `/think` takes `auto`, `on`, `off`, `low`, `medium` and `high`.

With it on, the header pulses **Pensando…** while the model works and becomes
**Pensou por 36s** when it finishes — clickable, with the whole draft inside.

Without this the bubble would sit empty for the entire reasoning phase, and
empty forever if the draft ate all of `max_tokens`. When that happens, the
panel says what went wrong instead of leaving the message blank.

---

## Agent mode

With it on, the model can **run commands on this machine** to answer. It chains
calls: looks at the state, decides the next step, and only then replies.

```
You: why did the metrics collector stop?

  ▸ shell   systemctl --user status hyde-widgets-collector      ok
  ▸ shell   journalctl --user -u hyde-widgets-collector -n 30   ok

The GPU path moved from card1 to card2 after the last boot,
so the collector can no longer find the sensor.
```

**Off by default.** The terminal button next to send turns it on, or
`/agent on`. Ollama only, with models that advertise `tools` — `ollama show`
tells you which.

> [!CAUTION]
> Tested **only with `qwen3.5:9b`**. Other models advertise `tools` and should
> work, but none were verified. The permission gate applies to all of them — it
> inspects the command, not the model — but how each one chains calls, whether
> it insists after a refusal, and how it reads output is behaviour I have not
> seen. Start with read-only requests.

### What it can run on its own

Read-only commands run immediately. Anything that changes the system shows the
exact command and waits for **Permitir** or **Negar**:

```
  ▸ shell   sed -i 's/card1/card2/' collector
            [ Negar ]  [ Permitir ]
```

The classification is a closed allowlist of read-only commands, applied to
**every segment** of the line — `ls | sh` does not pass, because `sh` is not on
the list. Redirection (`>`), command substitution (`` ` ``, `$(...)`), `sudo`,
`python3 -c` and writing subcommands (`git push`, `systemctl restart`,
`pacman -S`) all fall through to confirmation. So does an unknown command: the
rule errs towards asking, on purpose.

A refusal is not a dead end — the model receives it as the tool result and
explains what it would have done instead of retrying.

```bash
python3 tests/test_agente.py     # 66 commands against the safety gate
```

| Config | |
|---|---|
| `agent.enabled` | on or off (default: off) |
| `agent.max_steps` | ceiling on round trips per question (default: 8) |
| `agent.timeout` | seconds per command (default: 45) |

---

## History

Conversations saved and grouped by provider, with title, date and message
count. The header button lists and restores them; it keeps 40. Each row has a
delete button.

---

## Rendering

Markdown via [markdown-it-py](https://github.com/executablebooks/markdown-it-py)
— the same engine (Python port) the web frontends use: aligned tables, lists,
emphasis, code highlighted through GtkSourceView.

Mathematics is detected in four shapes, because models alternate between them:
`$$...$$`, `\[...\]`, ` ```math ` and paragraphs that are pure LaTeX.

Shell `$` is not mistaken for mathematics: `$HOME`, `$1`, `${VAR}` and `$5`
stay as text. While streaming, a half-finished formula is hidden until its
delimiter closes — it appears whole, in one go.

### Tests

```bash
python3 tests/test_bateria.py     # 43 cases: maths, Python, JS, shell
python3 tests/test_parser.py      # 21 cases: regressions and streaming
python3 tests/test_agente.py      # 66 cases: the agent safety gate
```

Every case came from a reply that showed up wrong on screen.

---

## Design decisions

<details>
<summary><b>Native GTK4, not KaTeX in WebKit</b></summary>

The panel streams token by token, and every delta would mean re-rendering an
HTML document — it flickers and costs dearly in a live stream. GTK widgets
update incrementally.

The price is that a formula is an image with no selectable text, hence
click-to-copy.

</details>

<details>
<summary><b>Incremental updates while streaming</b></summary>

Rebuilding the widget tree on every token made the message vanish and come
back. By preserving the prefix of already-closed segments, no stable widget is
destroyed: **0 rebuilds** on a reply with a formula and a table, against one
per token.

</details>

<details>
<summary><b>Provider availability comes from cache</b></summary>

Calling `available()` on send fired a **synchronous network probe on the UI
thread**, holding the user's own message back for up to 2 seconds.

</details>

<details>
<summary><b>Delimiters normalised before the parser</b></summary>

Markdown treats `\[` as an escape and eats the backslash, which made the
formula disappear mid-paragraph. Normalisation runs first, protecting anything
inside backticks.

</details>

---

## Dependencies

`gtk4-layer-shell` `python-gobject` `libadwaita` `gtksourceview5`
`python-matplotlib` `librsvg` `python-markdown-it` `python-mdit_py_plugins`
`python-pylatexenc` — all in the official Arch repositories.

---

## Licence

MIT

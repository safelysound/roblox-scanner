# Developer Tracker — `Suggest Developer` (YAGPDB) — Full Setup

Your request:
- **Button**: `Suggest Developer` — **Blue** (`style: primary`)
- **Modal**: Title `Suggest a Developer to be Added` — one field `Developer Username / ID / Profile Link`
- **On submit**: Post to `https://canary.discord.com/channels/1551328688547569754/1551394065491431494` (channel `1551394065491431494`) as

```
**New Developer Suggested**: <form answer>
<@475392041174564886>
```

You said: **YAGPDB** + **already have hosting** + **I'll create a webhook for that channel (no GitHub Secret needed)**. This guide gives you the *exact* YAGPDB setup that works.

> **Why not just add the button to the webhook embed?** Discord only routes **blue** (`custom_id`) button clicks to the *app that sent the message*. Your Developer Tracker is sent by a **webhook** (`155138715...` in `#1551385647703396433`), so YAGPDB would never receive the click — it shows `Interaction failed`.  
> **Working pattern (used here)**: Keep the tracker as webhook (updates every 5 min, pagination, Hunt-only). YAGPDB owns a *tiny companion message* right underneath it in the **same channel** (`#1551385647703396433` — Developer Tracker). That message holds the blue button; YAGPDB receives the click, shows the modal, and posts to the suggestions channel. Visually it looks like the tracker has a button.

---

## 1) Add YAGPDB to your server (if not already)

1. https://yagpdb.xyz → `Login with Discord` → select `1551328688547569754`
2. YAGPDB asks for permissions — accept (needs `Send Messages` + `Embed Links` in both `#1551385647703396433` and `1551394065491431494`)
3. In YAGPDB Dashboard, go to `Core` → ensure **YAGPDB** has a role high enough to post in those two channels.

## 2) Create the 3 Custom Commands

Go to **YAGPDB Dashboard > Custom Commands** (`https://yagpdb.xyz/manage/<guildId>/cc`)

You will create **3** commands. For each: click `Create a new Custom Command` → set the **Trigger** exactly as below → paste the **Code** → `Save`.

---

### CC #0 — One-time setup: send the blue button message

This is run **once** by you (admin) to create the persistent button that lives under the tracker.

- **Trigger Type**: `Command` → **Trigger**: `suggest_setup` (or `Slash Command` if you prefer)
- **Trigger on channel**: `1551385647703396433` (Developer Tracker channel) — or leave default and run it in that channel
- **Code** (paste exactly):

```
{{ $button := cbutton "label" "Suggest Developer" "custom_id" "suggest_developer" "style" "primary" }}
{{ $embed := cembed "title" "Developer Tracker" "description" "Have a developer to suggest? Click below." "color" 0 }}
{{ $msg := complexMessage "embed" $embed "buttons" (cslice $button) }}
{{ sendMessage nil $msg }}
```

- **Save** → in Discord, in `#1551385647703396433`, type `-suggest_setup` (or `/suggest_setup` if you made it a slash command). YAGPDB will post a small embed with the **blue `Suggest Developer` button**.  
- **Pin it** or keep it just below the tracker webhook. Leave it — YAGPDB owns it, so the button will keep working.

> Tip: If you want the button **without** the tiny embed (just a button row), use instead:
> ```
> {{ $button := cbutton "label" "Suggest Developer" "custom_id" "suggest_developer" "style" "primary" }}
> {{ sendMessage nil (complexMessage "content" " " "buttons" (cslice $button)) }}
> ```

---

### CC #1 — Button → Modal

- **Trigger Type**: `Interaction` → `Message Component` (sometimes labeled `Component` / `Button`)
- **Trigger** / **Custom ID**: `suggest_developer`  (must match the `custom_id` in CC#0 — **exact**)
- **Code**:

```
{{ $modal := cmodal
  "title" "Suggest a Developer to be Added"
  "custom_id" "suggest_developer_modal"
  "fields" (cslice
    (sdict "label" "Developer Username / ID / Profile Link" "placeholder" "e.g. 30685306 or https://www.roblox.com/users/30685306/profile" "required" true "style" 2 "max_length" 500)
  )
}}
{{ sendModal $modal }}
```

- **Save**. Now clicking the blue button will pop the modal.

> YAGPDB v2.66+ also supports the newer builder:
> ```
> {{ $ti := ctextInput "custom_id" "dev_input" "style" 2 "label" "Developer Username / ID / Profile Link" "placeholder" "e.g. 30685306 or https://..." "required" true "max_length" 500 }}
> {{ $label := clabel "label" "Developer Username / ID / Profile Link" "component" $ti }}
> {{ $mb := modalBuilder "suggest_developer_modal" "Suggest a Developer to be Added" $label }}
> {{ sendModal $mb }}
> ```
> Both work — use `cmodal` above for simplicity.

---

### CC #2 — Modal Submit → Post to suggestions channel

- **Trigger Type**: `Interaction` → `Modal Submission`
- **Trigger** / **Custom ID**: `suggest_developer_modal` (must match the modal's `custom_id` from CC#1)
- **Code**:

```
{{ $answer := index .Values 0 }}
{{ $channel := 1551394065491431494 }}
{{ $mention := "<@475392041174564886>" }}
{{ $content := printf "**New Developer Suggested**: %s\n%s" $answer $mention }}
{{ sendMessageNoEscape $channel $content }}
{{ sendResponseNoEscape nil (sdict "content" "✅ Suggestion submitted — thanks!" "flags" 64) }}
```

- **Save**.

**What this does**:
1. User clicks **Suggest Developer** (blue) → YAGPDB shows modal `Suggest a Developer to be Added` with `Developer Username / ID / Profile Link`.
2. On submit, YAGPDB posts to `#1551394065491431494` exactly as you requested:
   ```
   **New Developer Suggested**: <what they typed>
   <@475392041174564886>
   ```
3. User sees ephemeral `✅ Suggestion submitted — thanks!` (only they see it, `flags: 64`).

If you used the newer `modalBuilder` in CC#1, replace `index .Values 0` with how YAGPDB exposes values:
```
{{ $answer := (index .ModalValues "dev_input").value }}
```
or keep `cmodal` and `index .Values 0` — both are documented.

---

## 3) Test

1. In `#1551385647703396433`, click **Suggest Developer** → modal appears?
2. Fill `Developer Username / ID / Profile Link` → Submit
3. Check `#1551394065491431494` → you should see `**New Developer Suggested**: ...` + ping `<@475392041174564886>` (you)
4. If not, in YAGPDB Dashboard → `Custom Commands` → look at `Errors` tab for that CC.

---

## 4) Optional: Also add a Link button to the tracker webhook itself

Your `discord_updater.py` now supports buttons. If you *also* want the webhook embed to have a **grey Link button** (Discord restricts Link buttons to grey, not blue) that opens the suggestions channel directly, you can enable it in the GitHub workflow:

```yaml
python discord_updater.py roblox_scan_results_developers.json \
  --webhook "$DISCORD_WEBHOOK" --message-id-file discord_message_id_developers.txt \
  --title "Developer Tracker" --color "#000000" --emoji "<:developer:1551388816324169838>" \
  --button-label "Suggest Developer" --button-style link --button-url "https://canary.discord.com/channels/1551328688547569754/1551394065491431494"
```

But **blue + modal requires YAGPDB-owned message** (CC#0-#2 above). The webhook Link button would be grey and just opens the channel in browser — not a modal.

---

## 5) Files in this repo

- `yagpdb/CC0_setup_button_message.txt` — copy-paste for CC#0
- `yagpdb/CC1_button_to_modal.txt` — copy-paste for CC#1
- `yagpdb/CC2_modal_submit.txt` — copy-paste for CC#2
- `discord_updater.py` — now has `--button-label/--button-custom-id/--button-url/--button-style` (dry-run to preview)

For **Cloudflare Worker** alternative (blue button *on* the webhook message without YAGPDB, handled by your hosting), see `yagpdb/WORKER_ALTERNATIVE.md`.

---

## Troubleshooting

- `Interaction failed` on click → Button is on webhook message, not YAGPDB message. Use CC#0 to create a YAGPDB-owned message; ensure Custom ID matches exactly `suggest_developer`.
- Modal doesn't appear → Check CC#1 trigger is `Message Component` / `suggest_developer`, not `Command`.
- No post to suggestions channel → Check CC#2 trigger is `Modal Submission` / `suggest_developer_modal`, YAGPDB has `Send Messages` in `1551394065491431494`, and you used `sendMessageNoEscape` with the channel ID.
- Mention doesn't ping → Ensure `<@475392041174564886>` is in the `content` (not embed) — YAGPDB's `sendMessageNoEscape` preserves the mention.

You don't need a GitHub Secret webhook for the suggestions channel — YAGPDB posts as itself.

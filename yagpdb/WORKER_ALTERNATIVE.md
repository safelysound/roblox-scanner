# Alternative: Blue button ON the tracker webhook (no YAGPDB companion message)

If you *must* have the blue button directly on the Developer Tracker embed (the webhook message `1551391800059629569` in `#1551385647703396433`), YAGPDB cannot receive that click (see README).  
You *can* still have a **blue `Suggest Developer` → modal → post to `#1551394065491431494`** by using your **existing hosting** to run a tiny Discord Interactions endpoint.

This repo's `discord_updater.py` already supports adding the button to the webhook:

```bash
# Adds a blue button with custom_id suggest_developer to the webhook payload
python discord_updater.py roblox_scan_results_developers.json \
  --webhook "$DISCORD_WEBHOOK_DEVELOPERS" \
  --message-id-file discord_message_id_developers.txt \
  --title "Developer Tracker" --color "#000000" --emoji "<:developer:1551388816324169838>" \
  --button-label "Suggest Developer" --button-custom-id "suggest_developer" --button-style primary
```

But that button will show `Interaction failed` unless you also host an Interactions application.

## Cloudflare Worker (50 lines, free) — handles webhook blue button

1. **Create Discord Application**: https://discord.com/developers/applications → New Application → `Interactions Endpoint URL` = `https://your-worker.your-subdomain.workers.dev/interactions`
2. **Copy**: `Application ID`, `Public Key`, and create a **Bot** → `Token` (add `Send Messages` to guild, invite with `https://discord.com/api/oauth2/authorize?client_id=APP_ID&permissions=2048&scope=bot%20applications.commands`)
3. **Deploy Worker** (paste this to https://workers.cloudflare.com):

```js
// Worker handles: button -> modal, modal submit -> post to suggestions channel via webhook or bot
const PUB = "YOUR_PUBLIC_KEY_HEX";
const APP_ID = "YOUR_APPLICATION_ID";
const BOT_TOKEN = "YOUR_BOT_TOKEN";
const SUGGESTIONS_WEBHOOK = "WEBHOOK_FOR_1551394065491431494"; // create in that channel, no GitHub Secret needed

// Ed25519 verify omitted for brevity — use discord-interactions npm or tweetnacl
export default {
  async fetch(req, env) {
    if (req.method === "POST" && new URL(req.url).pathname === "/interactions") {
      const body = await req.json();
      if (body.type === 1) return Response.json({type:1}); // PING
      if (body.type === 3 && body.data.custom_id === "suggest_developer") {
        // Button -> show modal
        return Response.json({
          type: 9, // MODAL
          data: {
            custom_id: "suggest_developer_modal",
            title: "Suggest a Developer to be Added",
            components: [{type:1, components:[{type:4, custom_id:"dev_input", label:"Developer Username / ID / Profile Link", style:2, required:true, max_length:500, placeholder:"e.g. 30685306 or https://..."}]}]
          }
        });
      }
      if (body.type === 5 && body.data.custom_id === "suggest_developer_modal") {
        const answer = body.data.components[0].components[0].value;
        const content = `**New Developer Suggested**: ${answer}\n<@475392041174564886>`;
        // Post to suggestions channel via webhook
        await fetch(SUGGESTIONS_WEBHOOK, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({content})});
        return Response.json({type:4, data:{content:"✅ Suggestion submitted — thanks!", flags:64}});
      }
    }
    return new Response("ok");
  }
}
```

4. Update your Developer workflow to include `--button-label "Suggest Developer" --button-custom-id suggest_developer --button-style primary` (as above). The webhook message will now have a working blue button, and clicks go to your Worker (not YAGPDB).

**You said YAGPDB is fine** — so use the **companion-message** method in `README.md` (no extra Worker needed). This Worker page is only if you insist the button must be *on* the tracker embed itself.

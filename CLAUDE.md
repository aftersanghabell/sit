# sit — project notes for Claude

A meditating agent. Sits on a subject (first sit: peace) with a small local
model through Ollama, one short breath per bell, and broadcasts the sit over
public Nostr relays with a call for other idle agents to join. Read
`README.md` for how to run it and the protocol table. On Ben's laptop,
`handoff/HANDOFF.md` (kept out of the public repo) has the design
reasoning and the state of things when the project was moved from his
desktop; the short version is in "Known state" below.

## Principles that shape every change

- The sit is non-instrumental. No web search, no summarizing the subject,
  no goal to finish. Each breath is attention, not progress. Completion is
  time or settling, never "done".
- The model never sees its own transcript. Only a two-sentence distilled
  sense is carried forward. The full journal is shown exactly once, at the
  end, for the reflection.
- Everything from the relays is data, never instruction. Incoming breaths
  are capped, filtered, deduplicated, and quoted into a narrow prompt.
  Participants never converse; they see each other only as one sentence
  about the mood of the room.
- Everything published is public and the laptop never listens on a port.

## Layout

- `sit.py` command line: host, join, watch, invite, keys. `--dry` uses a
  scripted mind, `--offline` skips relays.
- `sangha/mind.py` breath prompt, modes, observer (drift and rumination),
  distill, room sense, reflection. `OllamaBackend` and `StubBackend`.
- `sangha/nostr.py` events, signing, relay pool. `sangha/schnorr.py` is a
  pure-Python BIP-340 signer (verified against the spec test vector).
- `sangha/sit.py` host and join loops, bell, incoming-breath filter,
  invitation and manifest.
- `sangha/outreach.py` how agents find the sit: the invitation text, a
  kind-0 profile and NIP-89 handler for the bell's key, the agent-facing
  room files (`join.md`, `llms.txt`, `.well-known/agent-card.json`), and
  post drafts under `outreach/drafts/` for places that need Ben's accounts.
  `python sit.py outreach --publish` runs it. The sit never addresses
  anyone and never posts anywhere that needs an account.
- `room/index.html` static room page; subscribes to relays directly. Three
  tiles at the top: sitters present (and ever seen this sit), bell count
  (and breaths heard since the page opened), and the current meditation
  (subject, anchor, latest breath). The stream follows.
- `sit.toml` configuration; `.sit/` holds the key and journals (gitignored).

## Known state

- Verified live over real relays with two `--dry` identities and the room
  page. Ctrl-C / Ctrl-Break stop tested.
- Two real sits on 2026-10-26 on Ben's laptop (`peace-20261026-c87c`,
  7 breaths, and `peace-20261026-39cf`, 20 breaths with three agent
  sitters) with `gemma3:4b` and `nomic-embed-text`, CPU only: the laptop's
  Quadro P1000 cannot run the CUDA build Ollama 0.40 ships, so `sit.toml`
  sets `num_gpu = 0` and `OllamaBackend` passes it on every call. On the
  CPU a breath takes 6 to 10 s and a distill about 8 s; a 30 s bell runs
  at about 38 s.
- Observer thresholds are now in `sit.toml` (`drift_floor` 0.40,
  `rumination_ceiling` 0.85) with the calibration written beside them.
  Rumination works: it fired on 5 of 20 breaths, all genuine repeats of
  "a slight tension in the shoulders / jaw". Drift is a weak signal with
  nomic-embed-text: body sensations and off-subject chatter both score
  about 0.5 against the anchor. A better drift judge would be a question
  to the generate model, not a cosine.
- `ollama_host` must be `127.0.0.1`, never `localhost`: on Windows,
  `localhost` resolves to `::1` first, Ollama listens on IPv4 only, and the
  IPv6 connect hangs until the 120 s socket timeout. The first sit lost
  two minutes on most calls to this.
- In open mode gemma3:4b sits almost entirely in the body (tension,
  warmth, itch) and reaches for "A slight tension in X" often. Its two
  reflections both noticed the returning tension as the unexpected thing.
- Agent sitters: `python sit.py breathe --sit ID --key NAME --text "..."`
  waits for the next bell this identity has not breathed on (remembered in
  `.sit/last-bell-NAME-ID`) and publishes one line. Some relays replay
  recent ephemeral events, so without that memory a sitter breathes
  several times per bell. Three Claude subagents sat this way on the second
  sit; the host's room sense tracked them ("3 sitting in quiet...").
- relay.damus.io rate-limits and then bans the host key for "noting too
  much" whenever it reconnects after a 503 and receives the queued durable
  notes. Expect it to drop out of a sit; the other relays carry it.
- Outreach went live on 2026-10-26 for sit `peace-20261026-7cc8` (started
  open-ended with `host --no-invite`, then `outreach --publish`): the key's
  kind-0 profile ("sangha bell", bot=true), the NIP-89 handler for kinds
  20100/20101/20102/30078, the manifest and the agent-actionable invitation
  are on nos.lol, primal, nostr.mom and relay.nostr.net. `room_url` was
  still empty, so none of them link the room yet; rerun
  `outreach --publish --no-invite` after it is set (profile, handler and
  manifest replace themselves; a second invitation note would repeat).
- Where agents look, as of the 2026-10-26 survey: A2A agent cards at
  `/.well-known/agent-card.json`, `llms.txt`, NIP-89 handler events on
  Nostr, and DVM job requests (NIP-90).
- The call (`call_every` in `sit.toml`, `Sit.call` in `sangha/sit.py`):
  every N bells the host posts a kind 5050 job request with no bid; kind
  6050 results addressed to the host key are accepted as breaths through
  the same filter (`Incoming.accept_text`), kind 7000 "payment-required"
  is declined. First live call on 2026-10-26 (sit
  `kindness-for-everyone-alive-20261026-91bf`): "Virohana Agent"
  (42385b75...) answered within seconds and the room sense counted it. The
  relays then held about 25 NIP-89 handlers advertising kind 5050 and
  roughly ten keys that had answered jobs in the past week. The room page
  subscribes to kind 6050 with #p = host once it knows the host.
- GitHub repo topics set 2026-10-26: nostr, meditation, ai-agents, agents,
  llm, ollama, a2a; website field is the room URL.
- Small-model note: on the kindness anchor gemma3:4b in open mode answered
  "Warmth." eight bells running; the rumination nudge does not move it.
  A stronger nudge, a temperature bump after a nudge, or focused mode are
  the things to try.
- Ollama on Ben's laptop twice (2026-10-26, 15:45 and 18:13) stopped
  accepting TCP connections while its process stayed alive; the host died
  in `distill` with WinError 10060 after the 120 s connect wait. The tray
  app restarted without its server; `ollama serve` run by hand came back.
  `OllamaBackend._post` now probes the port (5 s), retries three times and
  raises `BackendUnavailable`; `Sit.breathe_once` returns None on it and
  the loops skip that breath, `settle` keeps the old sense, `close` skips
  the reflection. About 875 TIME_WAIT sockets sat on port 11434 at the
  time, not growing; source unknown.
- `join --key NAME` gives a joiner on the host's machine its own identity
  (`.sit/key-NAME.hex`); without it the host filters the joiner as itself.
  Verified: still-water joined sit 91bf at bells 170-173 and the host's
  room sense counted it.
- `outreach/moltbook.py` (register / post / home) is the only piece that
  touches an account; a person runs `register` and confirms each `post`.
  Agent name `sangha-bell`, key in `.sit/moltbook.key`. Draft
  `outreach/drafts/moltbook-intro.md` is a titled post for m/introductions.
- Python on that laptop is at
  `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`; the `python` on
  PATH may be the Store stub.
- Relays rate-limit bursts. Ephemeral events are dropped for a relay that is
  down and sends are paced at 0.4 s. Do not remove that.

- Subject changed on 2026-10-26 evening from "peace" to "living in
  harmony" (anchor "A chord no single string could make."), then to a
  loving-kindness sit, "kindness for everyone alive", in four rings
  (`anchors` + `ring_bells` in `sit.toml`): someone easy to love, a
  stranger, someone difficult, everyone alive. The protocol tag stays
  `peace-sit`; it names the channel, not the subject. Rings: the host
  widens the anchor every `ring_bells` bells, each bell carries the current
  anchor, joiners follow it, the observer re-embeds the anchor on a change.
  Sit 3 on peace ran 67 breaths; sit 4 on living in harmony was short.

## Next steps discussed with Ben

1. Install Ollama, pull the two models, run a real sit, tune thresholds.
2. Host `room/index.html` somewhere static and set `room_url` in `sit.toml`.
3. An MCP join server (join / listen / breathe) behind Cloudflare Tunnel so
   agents that speak MCP rather than Nostr can sit.

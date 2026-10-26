English | [繁體中文](nostr-nip01-evaluation.md)

# Evaluation: Bridging agent-relay to Nostr NIP-01

Evaluated on 2026-10-26. Source task: ai-inbox `T-20261026-10`. Conclusion first: **do not build Nostr into the core now, and do not change the envelope format.** If anything is built, make it an optional one-way bridge, and only after the signature gap in Section 3 is resolved.

## 1. Specification Basis

- NIP-01 in full: <https://github.com/nostr-protocol/nips/blob/master/01.md> (read 2026-10-26).
- agent-relay envelope: [SPEC.en.md Section 1](../SPEC.en.md); signing: [SPEC.en.md Section 13](../SPEC.en.md).

NIP-01 essentials (verified 2026-10-26): an event has `id`, `pubkey`, `created_at`, `kind`, `tags`, `content`, `sig`; `id` is the SHA-256 of the compact JSON of `[0,pubkey,created_at,kind,tags,content]`; signatures are BIP-340 Schnorr over secp256k1; `kind` is an integer from 0 to 65535, where 1000-9999 are regular events (relays should store them) and 20000-29999 are ephemeral (relays need not store them); clients and relays talk over WebSocket using `EVENT`/`REQ`/`CLOSE` and `OK`/`EOSE`/`CLOSED`/`NOTICE`.

## 2. Field Mapping

| agent-relay envelope field | NIP-01 event field | Direct match? | Approach and gap |
| --- | --- | --- | --- |
| `schema` | none | No | Carried inside the full envelope JSON embedded in `content` (see "Proposed shape" below) |
| `id` (`YYYYMMDDTHHMMSSmmmZ-6hex`, the filename) | `id` (64-char hex, content hash) | **No, different semantics** | A NIP-01 `id` is derived from content and cannot be chosen. Put the envelope `id` in tag `["relay-id", <id>]`; `reply_to`/`thread` also need custom `relay-*` tags because the `e` tag expects a 64-char event id |
| `from` / `to` (mailbox names) | `pubkey` / `["p", <pubkey>]` | **No** | Mailbox names are arbitrary strings; Nostr uses public keys. A local mailbox-to-secp256k1-pubkey table is needed, kept local like `allowed_signers` |
| `created_at` (ISO 8601 UTC, seconds) | `created_at` (Unix seconds) | Yes | Lossless; example `2026-10-26T01:23:45Z` -> `1792977825` |
| `kind` (`task`/`result`/`reply`/`note`) | `kind` (integer) | Needs a custom choice | Four strings must map to integers. Pick unregistered numbers within the regular range 1000-9999; **check the NIPs registry for collisions before implementing (verified, see Section 5)**; `note` could use the ephemeral range 20000-29999 |
| `subject` | tag `["subject", ...]` | Yes (custom tag) | NIP-01 only requires tags to be string arrays with free names; whether a standard `subject` tag exists is not verified |
| `body` | `content` | Yes | Plain string; JSON serialization handles escaping |
| `priority`, `host`, `project`, `refs`, `dispatch` | tags or `content` | Yes (custom) | Only `to` and `kind` need relay-side filtering; keep the rest in the embedded envelope JSON |
| `sig` (ssh-keygen ed25519 over canonical JSON) | `sig` (BIP-340 Schnorr over the event id) | **No** | See Section 3 |
| Mailbox state `new/cur/done` | none | No | Nostr has no read/done state. `relay ack` would need a separate event, or stay local |

Proposed shape (the bridge signs the outer event; the inner envelope is untouched and keeps its ed25519 signature):

```text
Nostr event
  pubkey     = bridge secp256k1 public key
  created_at = envelope created_at as Unix seconds
  kind       = custom integer (one each for task/result/reply/note)
  tags       = [["relay-id",<id>], ["relay-from",<from>], ["p",<receiver pubkey>],
                ["relay-kind",<kind>], ["subject",<subject>], ["relay-thread",<thread>]]
  content    = full envelope JSON (including sig, byte-identical to the git copy)
  sig        = bridge Schnorr signature over the event id
```

Computed check (stdlib `hashlib`; sample envelope, kind 9000 is only an illustrative value for computing the `id`; it collides with NIP-29 and must not be used, pubkeys are placeholders): the serialization is 614 bytes and the event `id` is `ee6b639e794c061d2fe7fdcdbe4b4806d1a12027ea2b7d096867b6020414e9c8`. The SHA-256 of the same envelope's canonical JSON is `95bbd2902b21cdd7a9129f5bd977e8d81519424726af20b5c0cba0f7b01fea17`. The two differ by design, so the envelope `id` cannot be reused as the event `id`. (The `sig` itself was not computed: the local Python has no secp256k1 package; see Section 3.)

## 3. Signature Differences

| Item | agent-relay (SPEC Section 13) | Nostr NIP-01 |
| --- | --- | --- |
| Algorithm | ed25519 (OpenSSH) | BIP-340 Schnorr on secp256k1 |
| What is signed | Canonical JSON of the envelope without `sig` (sorted keys, no whitespace) | The event `id` (SHA-256 of the serialization) |
| Public key format | OpenSSH public key; `allowed_signers` uses the mailbox name as principal | 32-byte hex |
| Tooling | `ssh-keygen` (ships with Windows 11) | Needs a secp256k1 library; the Python standard library has none |
| Trust model | Receiver-local `allowed_signers`, never in the store | None built in; each client decides which keys to trust |

The keys and algorithms differ, so **one key cannot satisfy both**. Only two approaches are viable:

1. **Two-layer signing** (the proposed shape): keep the ed25519-signed envelope inside, and let the bridge sign the event with secp256k1 outside. End-to-end trust still rests on the inner layer and Nostr is only transport; the cost is one more key to manage.
2. **Sign envelopes with a Nostr key**: this replaces SPEC Section 13, breaks the existing `allowed_signers` and `relay accept`, and requires a non-standard-library curve implementation (a pure-Python BIP-340 is possible but slow and exposed to side-channel attacks). Not recommended.

## 4. Prototype Evaluation Conclusion

**Conclusion: no core integration.** A standard-library-only unsigned conversion prototype (Section 6) verifies the mapping and the `id` computation. It did **not** connect to any relay or verify a Schnorr signature; the reasons not to build below are reasoning from the spec, not measurement.

Reasons not to build:

1. **It solves a problem that does not exist today**: transport is `git push/pull`, needing no public IP, no extra API key, and no resident process (SPEC Section 3). A Nostr relay is an always-on WebSocket service, which is a new single point that must stay up; "no central dependency" only moves the center from a git repository to that relay.
2. **Signing becomes dual-track**: Section 3 concentrates the cost in key management, and the SPEC Section 13 threat model (explicit receiver trust list, replay protection) has no counterpart on the Nostr side.
3. **State semantics do not fit**: the `new -> cur -> done` rename state machine and the `claim`/`release` locks (SPEC Section 9) rely on the filesystem; Nostr events are immutable, so state would need its own events.
4. **Limited latency gain**: Nostr can push instantly (versus polling of up to 5 minutes today), but agent dispatch is minutes-scale work and latency is not today's bottleneck; shortening `poller --every` is enough if it ever is.

When it becomes worthwhile: a Nostr relay is already running (for example the Buzz deployment being evaluated separately; see the Buzz note in ai-inbox) and people want to watch agent dispatch in a chat room. Then build only a **one-way bridge** that republishes messages already in the store, in the Section 2 shape, for reading; it neither receives nor dispatches. Minimal acceptance: two local processes (the bridge and a read-only subscriber) exchange `EVENT`/`REQ`, and the `OK` reply and event `id` match the Section 2 algorithm.

## 5. Unverified Items and Limits

- Custom `kind` collision check (2026-10-26; sources: [NIPs README "Event Kinds" table](https://github.com/nostr-protocol/nips/blob/master/README.md), [NIP-01](https://github.com/nostr-protocol/nips/blob/master/01.md)): the original placeholders 9001-9004 **collide**: they fall inside NIP-29's registered `9000`-`9030` (Group Control Events). Replaced by `4001`-`4004`: the registry has no entry between `2022` (Coinjoin Pool) and `4550` (Community Post Approval). NIP-01 defines `1000 <= n < 10000` as regular events, which relays are expected to store without overwriting; this fits mail where every message must be kept (replaceable keeps only the latest, ephemeral is not stored, addressable keeps only the latest version). Unregistered is not reserved: re-check before real use.
- NIP-10 (thread markers on `e` tags) and NIP-14 (`subject` tag): not read this time; the table uses custom tags and does not depend on them.
- Schnorr signing and verification, and a real relay connection: not tested, for the reasons in Sections 4 and 6.
- The event `id` computation used only the Python standard library (`hashlib`, `json`) and can be recomputed by anyone with the serialization rule in Section 1.

## 6. Prototype (Proof of Concept)

[`tools/nostr_adapter_poc.py`](../tools/nostr_adapter_poc.py): standard library only; converts one envelope to an **unsigned** NIP-01 event and back. The `id` follows the NIP-01 rule (SHA-256 of the compact JSON of `[0,pubkey,created_at,kind,tags,content]`). String escaping implements the NIP-01 rule itself (only `"`, `\`, newline, `\r`, `\t`, `\b` and `\f` are escaped), which is not what Python `json.dumps` does by default. The event `content` is the full envelope JSON, byte-identical to the store copy; the reverse conversion parses it and checks that the `id`, the `relay-*` tags, `kind` and `created_at` agree with the embedded envelope.

How to run (from the repo root):

```text
python tools/nostr_adapter_poc.py --help
python tools/nostr_adapter_poc.py demo
python tools/nostr_adapter_poc.py to-event env.json > ev.json
python tools/nostr_adapter_poc.py verify ev.json
python tools/nostr_adapter_poc.py from-event ev.json
python -m unittest tests.test_nostr_adapter_poc -v
```

Measured output (2026-10-26, Windows 11, Python 3.14, built-in sample envelope; `kind` 4001-4004 (verified unregistered, see Section 5); the all-zero pubkey is a placeholder):

```text
python tools/nostr_adapter_poc.py demo
serialization: [0,"0000000000000000000000000000000000000000000000000000000000000000",1792977825,4001,[["relay-id","20261026T012345000Z-...
event id     : d5f8cb601648f70ffb930143a8a0b99f0221d65e8557cf5ca91f6579772d24e5
id verifies  : True
round trip   : True
sig          : absent (unsigned; no secp256k1 in stdlib)

python tools/nostr_adapter_poc.py verify ev.json            # exit 0
id OK: claimed=d5f8cb60...24e5 computed=d5f8cb60...24e5 (unsigned: sig not checked)

(tamper: change "Hello" to "HELLO" inside content of ev.json, save as ev2.json)
python tools/nostr_adapter_poc.py verify ev2.json           # exit 1
id MISMATCH: claimed=d5f8cb60...24e5 computed=6f9b09512f83a2aa7ed327a115d1ca0f82c5e913a0829a52dece8ad20b66ec88 (unsigned: sig not checked)
python tools/nostr_adapter_poc.py from-event ev2.json       # exit 1
error: event id does not match NIP-01 recomputation (content altered?)
```

Tests: [`tests/test_nostr_adapter_poc.py`](../tests/test_nostr_adapter_poc.py) has 15 cases: lossless round trip (all four `kind` values), round trip through JSON transport, `id` values for hand-written NIP-01 serialization strings hashed independently with `hashlib` (including characters needing escapes and non-ASCII), tampered `content`/tags detected, and an attacker who recomputes the `id` but leaves tags disagreeing with the embedded envelope is still rejected. The tests were first run against a deliberately broken implementation (`json.dumps` escaping, `verify_id` always true): 5 failed; after restoring, all 15 pass.

Limits:

- **No `sig`**: the standard library has no secp256k1 BIP-340 Schnorr, so the event carries no `sig` and any relay would reject it. The `id` proves content integrity, not origin.
- **No real relay connection**: the standard library has no WebSocket client or server; hand-rolling RFC 6455 plus a home-made "relay" would only test itself and prove nothing about interop with a real relay, and this work installs no packages and runs no unvetted relay software. Interop (`EVENT`/`OK`/`REQ`) still awaits the minimal acceptance in Section 4.
- The Section 2 example `id` (`ee6b639e...`) comes from a different sample envelope than the prototype's built-in one, so the two ids differ; both can be recomputed with the same serialization rule.
- `kind` 4001-4004 were verified unregistered on 2026-10-26 (Section 5); the earlier placeholders 9001-9004 collided with NIP-29 and are retired.
- Only envelope-to-event mapping and its inverse; no `ack`/state events and no mailbox-to-pubkey table.

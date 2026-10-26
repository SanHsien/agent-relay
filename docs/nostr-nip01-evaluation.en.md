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
| `kind` (`task`/`result`/`reply`/`note`) | `kind` (integer) | Needs a custom choice | Four strings must map to integers. Pick unregistered numbers within the regular range 1000-9999; **check the NIPs registry for collisions before implementing (not verified)**; `note` could use the ephemeral range 20000-29999 |
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

Computed check (stdlib `hashlib`; sample envelope, kind 9000 is a placeholder, pubkeys are placeholders): the serialization is 614 bytes and the event `id` is `ee6b639e794c061d2fe7fdcdbe4b4806d1a12027ea2b7d096867b6020414e9c8`. The SHA-256 of the same envelope's canonical JSON is `95bbd2902b21cdd7a9129f5bd977e8d81519424726af20b5c0cba0f7b01fea17`. The two differ by design, so the envelope `id` cannot be reused as the event `id`. (The `sig` itself was not computed: the local Python has no secp256k1 package; see Section 3.)

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

**Conclusion: no core integration, and no prototype for now.** This work covers only the mapping analysis and an event-`id` computation. It did **not** build an adapter, connect to any relay, or verify a Schnorr signature; what follows is reasoning from the spec, not measurement.

Reasons not to build:

1. **It solves a problem that does not exist today**: transport is `git push/pull`, needing no public IP, no extra API key, and no resident process (SPEC Section 3). A Nostr relay is an always-on WebSocket service, which is a new single point that must stay up; "no central dependency" only moves the center from a git repository to that relay.
2. **Signing becomes dual-track**: Section 3 concentrates the cost in key management, and the SPEC Section 13 threat model (explicit receiver trust list, replay protection) has no counterpart on the Nostr side.
3. **State semantics do not fit**: the `new -> cur -> done` rename state machine and the `claim`/`release` locks (SPEC Section 9) rely on the filesystem; Nostr events are immutable, so state would need its own events.
4. **Limited latency gain**: Nostr can push instantly (versus polling of up to 5 minutes today), but agent dispatch is minutes-scale work and latency is not today's bottleneck; shortening `poller --every` is enough if it ever is.

When it becomes worthwhile: a Nostr relay is already running (for example the Buzz deployment being evaluated separately; see the Buzz note in ai-inbox) and people want to watch agent dispatch in a chat room. Then build only a **one-way bridge** that republishes messages already in the store, in the Section 2 shape, for reading; it neither receives nor dispatches. Minimal acceptance: two local processes (the bridge and a read-only subscriber) exchange `EVENT`/`REQ`, and the `OK` reply and event `id` match the Section 2 algorithm.

## 5. Unverified Items and Limits

- Whether a chosen custom `kind` number collides with a registered one: not verified.
- NIP-10 (thread markers on `e` tags) and NIP-14 (`subject` tag): not read this time; the table uses custom tags and does not depend on them.
- Schnorr signing and verification, and a real relay connection: not tested, for the reason in Section 4.
- The event `id` computation used only the Python standard library (`hashlib`, `json`) and can be recomputed by anyone with the serialization rule in Section 1.

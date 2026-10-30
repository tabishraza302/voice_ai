# Voice Agent Studio

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The local studio supports a single operator configuring and testing voice agents on an Apple Silicon Mac. Platform mode serves authenticated customers in owner-scoped workspaces; platform administrators use separate administration screens.

## Product Purpose

The local studio manages bilingual English and Hindi voice agents through a local pipeline. Platform mode lets each customer create, configure, validate, publish, and text-test only their own agents.

## Capabilities and Constraints

- Local mode calls the local FastAPI service for agent, prompt, and model catalog operations; voice sessions use browser WebRTC and microphone access.
- Platform mode uses the authenticated platform API for owner-scoped agent lists, versioned templates, manual or generated drafts, compare-and-swap flow saves, validation, immutable publication, version restore, and enable/disable. Customers also see their real wallet balance and ledger, persisted call history and usage, pinned config snapshots, and owner-visible transcript state; admins get searchable inventories and audited operational controls.
- Step, graph, and JSON views edit the same versioned flow. Chat Test is text-only and pinned to a server-provided draft snapshot or immutable published artifact; it does not consume a voice slot.
- Generated proposals are bounded, reviewed by the customer, and never saved or published without an explicit action. Unknown business facts remain visible for confirmation.
- Platform Overview and customer billing/history views use persisted wallet, ledger, session and call-budget facts. Admin views use real platform records, immutable tariff history, admission policy, transcript review and audit data. They do not show guessed balances, fake activity, or placeholder operational status.
- Customers cannot configure arbitrary server URLs or credentials or dynamically install provider code. Runtime claims must come from live state or measured data; do not present configured values as observed performance.
- The local runtime is intended to remain air-gapped.

## Evidence on Hand

The repository contains measured performance scripts and recorded results; the browser studio does not currently query live latency or memory measurements.

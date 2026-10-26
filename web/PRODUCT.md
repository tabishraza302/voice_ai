# Voice Agent Studio

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Repository context identifies a single local operator configuring and testing the voice agent on an Apple Silicon Mac. No other user roles are specified.

## Product Purpose

The studio lets the operator manage bilingual English and Hindi voice agents, edit the shared base prompt, and test a selected agent through a browser voice session.

## Capabilities and Constraints

- The browser UI calls the local FastAPI service for agent, prompt, and model catalog operations.
- Voice sessions use browser WebRTC and microphone access with the local pipeline.
- Agent configuration includes prompt, LLM, STT provider, and English/Hindi voice selections.
- Runtime claims must come from live state or measured data; do not present configured values as observed performance.
- The runtime is intended to remain air-gapped.

## Evidence on Hand

The repository contains measured performance scripts and recorded results; the browser studio does not currently query live latency or memory measurements.

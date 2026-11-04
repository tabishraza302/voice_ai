# AGENTS.md — working on this repository

Last reviewed: 2026-10-19.

This file is the contract for anyone (human or agent) changing this repository. It is short on purpose: the rules below are the ones that have actually prevented mistakes, not a restatement of the code.

## What this project is

A voice agent with an AI observability layer, on one Apple Silicon MacBook Air (M5, 16 GB unified
memory). The README's
numbers are measured on this machine, not copied from a paper.

## The rules that matter

### 1. Measure before you assume

When asked for any benchmark, model, TTS, STT or anything else, measure it locally instead of assuming or getting the result from the internet.

### 2. Keep the three layers swappable

STT, TTS and LLM are modular which means that we can add or remove them as needed without affecting any other part of the application or affecting the platform.

### 3. A new backend is a new implementation, not a special case

Never add a branch like `if settings.stt_provider == "x"` inside the
pipeline. That is the thing the registries exist to prevent.

When using a pstack skill or playbook in omp, first read
`.omp/pstack-omp.md` for the tool and harness mappings. The adapter applies
to delegated pstack roles too.

## Before you call a change done

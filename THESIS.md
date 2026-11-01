# The thesis behind this project

> **In mining maintenance, the record a technician writes is the ground truth for everything that follows:
> reliability analysis, spare-parts planning, warranty claims and predictive models. Yet it is captured at the
> worst moment, in the worst conditions, with the worst tools. A voice-first assistant that keeps the record
> structured where structure pays, and free where words carry the detail, can capture more usable information
> in the same time as a paper form, with less effort and fatigue, and turn maintenance records into data that
> analytics can trust.**

This document explains the problem, the design principles that follow from it, how the app implements them,
and how the claim can be tested.

---

## 1. The problem

### 1.1 Maintenance records are the ground truth, and they are weak

Reading several hundred work-order texts and lubricant records of large wheel loaders in an operating mine
showed patterns that are common across the industry:

- **Written after the fact.** Corrective orders are often written after the repair; preventive orders are
  created weeks before the work. The date of the record is not the date of the event.
- **Several jobs in one record.** An oil change can sit inside a preventive-service order, invisible to any
  structured query.
- **Missing structure.** Top-up (refill) records with an empty compartment field; quantities only inside free
  text; an hour-meter value copied from a counter that had not been updated.
- **Many names for one thing.** The same component is named differently by the ERP, the oil laboratory and
  the workshop.
- **Paper inspection forms.** A tick per row, a comments column that is mostly empty, and a transcription into
  the ERP that happens later, partially or never.

### 1.2 Why it matters for predictive maintenance

Predictive models are limited more by the quality and timing of their labels than by their algorithms.
Condition monitoring (oil analysis every few hundred hours, on-board alarms, sensor data) can only be linked to
what technicians did if each action is recorded **when** it happened (hour meter), **where** (compartment,
position) and **how much** (litres, kilograms). Top-ups between two oil samples, for example, describe
consumption and leaks, but this information is usually lost or buried in narrative text.

### 1.3 Human factors

Gloves, dust, noise, heat, protective equipment and fatigue at the end of a long shift make writing slow and
typing on a tablet painful. Speech is the natural channel, but technicians use a local technical vocabulary
(for example the Spanish workshop names of bucket wear parts) that general-purpose dictation handles poorly,
and a misheard "no" or "sin" (without) reverses the meaning of a finding.

### 1.4 Connectivity and data ownership

Workshops and pits often have weak or no connectivity, so cloud-only tools fail when they are needed most.
Maintenance records can also be confidential, so sending them to third-party services is not always acceptable.

---

## 2. Design principles

| # | Principle | How the app implements it |
|---|---|---|
| 1 | **Structure where it pays, voice where it helps.** | One tap per checklist item (OK / Monitor / Fault / N/A), one-tap defect chips, position and compartment dropdowns, top-up quantities with units; dictated comments for everything else. |
| 2 | **Completeness without assumption.** | Every item must be marked explicitly; nothing defaults to "OK". Items can be done in any order (each technician walks around the machine their own way) and "next pending" finds what is left. |
| 3 | **Context captured at the source.** | Hour meter next to date and time, checked against the last saved value; machine from the asset list; a timestamp on every item when it is marked. |
| 4 | **Evidence and correction.** | The raw transcript and the audio stay next to the edited text; negations, numbers and sides are highlighted, because those are the errors that change meaning. |
| 5 | **Offline first, local by default.** | Whisper large-v3 (int8, CTranslate2) runs on a laptop CPU; records go to SQLite and an Excel workbook and wait in a queue for the ERP. Nothing leaves the computer unless the user chooses cloud dictation. |
| 6 | **The engine choice is an explicit trade-off.** | Local Whisper (private, offline, about 10 s per dictation on a CPU) or Wispr Flow (about 1–2 s, cloud, needs internet). The user chooses per situation; the record shows which engine produced each text. |
| 7 | **Low friction.** | A desktop app that opens with a double-click, large touch targets, dropdowns instead of typing, push-to-talk or tap-to-talk, a keyboard shortcut, Spanish and English screens (instead of machine translation), automatic draft saving. |
| 8 | **Safety is surfaced, never decided.** | Safety-critical items are flagged; a fault on one shows a warning until the machine is grounded. The technician decides the machine status. |

---

## 3. The checklist as a measuring instrument

The daily inspection starts from the OEM's generic wheel-loader inspection form and keeps its four sections
and their order (from the ground, engine compartment, on the machine outside the cab, inside the cab).
It is then aligned with the CAT 994K and with what the fleet's corrective work orders show matters:

- **Duplicates merged.** Transmission and transmission oil; differential and final-drive oil and the axles;
  steps and handholds; lights that appeared in two sections.
- **Items the model does not have removed.** The air tank for air brakes.
- **Items the work orders point to added.** Pins and bushings, automatic lubrication (grease in kg),
  articulation and steering cylinders, turbochargers and exhaust, guards, on-board monitoring alarms with their
  code, service and parking brake test, steering test, radio.
- **Defect chips.** Each item's chips follow what corrective orders describe most often: leaks, clogged air
  filters, low or high levels, cut grease lines, monitoring alarms.

The form therefore asks, at the moment of inspection, for the information the organisation needs later.
It is a JSON file (`data/checklist_994k.json`), so it can be adapted to other models.

---

## 4. Hypotheses and how to test them

| Hypothesis | Measure | Source |
|---|---|---|
| **H1 – Time.** An inspection with the app takes no longer than with the paper form. | Minutes from first to last item. | Per-item timestamps; stopwatch for paper. |
| **H2 – Information.** The app captures more structured information per inspection. | Defects, positions, quantities and codes per inspection; words of comment. | The Excel *Inspeccion* and *Rellenos* sheets vs. scanned paper forms. |
| **H3 – Completeness.** Records are complete and usable. | Share of items explicitly marked; share of top-ups with compartment + quantity + hour meter, compared with ERP refill records. | Database vs. ERP extracts. |
| **H4 – Accuracy.** Dictation is accurate enough on domain language. | Word error rate on domain terms and on meaning-critical words (negations, sides, numbers); edits between raw transcript and final text; local vs. cloud engine. | *Grabaciones* sheet with audio. |
| **H5 – Effort.** Less workload and better usability than paper. | NASA-TLX workload and SUS usability scores; preference. | Questionnaires after use. |
| **H6 – Downstream value.** Records link to condition monitoring. | Share of oil-analysis results and on-board alarms that can be matched to a recorded action; label quality for predictive models. | Join of records with condition-monitoring data by machine and hour meter. |

A within-subject crossover design (the same technicians alternate paper and app on real daily inspections)
controls for individual differences, and most measures come from the app's own records.

---

## 5. Limitations and open questions

- **Speech recognition errors remain.** For example, "Frenos sin novedades" (brakes without findings) was heard
  as "Frenos y novedades". Keeping the raw transcript and the audio makes errors visible, but does not remove them.
- **Latency on a CPU.** About 10 s per dictation with the local model; faster models or hardware would help.
- **Cloud dictation sends audio to a third party.** Its use needs a data-governance decision.
- **"Mark pending as OK" saves taps but can enable ticking without looking.** The per-item timestamps make this
  visible in the data.
- **One machine model so far.** The checklist is data, not code, so other models can be added.
- **No ERP integration yet.** Records wait in a local queue.
- **Not yet validated in a field study.** That is the next step (section 4).

## 6. Roadmap

ERP (SAP) notification and order creation from saved records; HTTPS so tablets and phones can use the
microphone; photos of defects; narrating several items in one recording with on-device language
understanding; faster on-device speech models; a field evaluation following section 4.

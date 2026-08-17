# Fine-tuning Strategy

EPIS is model-agnostic. Fine-tuning is used to improve conversational consistency, not to make a checkpoint the sole source of identity.

The intended workflow is:
1. collect explicit feedback and approved conversation examples;
2. curate a portable JSONL dataset;
3. fine-tune/instruction-tune an open-weight base model (for example with LoRA/QLoRA);
4. evaluate it against a fixed conversational benchmark;
5. keep values, memory, and identity files external so a future base model can replace it.

The public repository ships only synthetic sample data.

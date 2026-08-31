# Hosting the pinned NLP assets

Use this only when packaging a ResearchRamp release with a Tencent COS, Alibaba OSS, or equivalent HTTPS static asset origin.

## Required object layout

Let the public HTTPS bucket or CDN origin be `<endpoint>`. Upload these objects without changing their filenames:

```text
<endpoint>/python/en_core_web_sm-3.8.0-py3-none-any.whl
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/1_Pooling/config.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/config.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/config_sentence_transformers.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/model.safetensors
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/modules.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/sentence_bert_config.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/special_tokens_map.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/tokenizer.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/tokenizer_config.json
<endpoint>/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/vocab.txt
```

The desktop installer uses ordinary HTTPS GET requests. Bucket listing and browser CORS are not required. Public read access is acceptable because both upstream assets are public; the paid product value must not depend on keeping these third-party model files secret.

## Release configuration

Set `asset_endpoint` in `embedding-model-manifest.json` to the HTTPS origin, with no trailing path beyond the bucket/CDN root. Do not change the pinned revision or hashes without deliberately rebuilding and revalidating the model release.

Keep the Apache-2.0 model license and spaCy model's MIT license/attribution with the distributed asset set. The installer validates the official spaCy wheel hash and ten model-file hashes before inference.

Before release, run the installer into a new temporary `--venv-dir` and `--model-dir`, with `RESEARCHRAMP_MODEL_ENDPOINT=<endpoint>`, then run it again without `--install` to prove offline reuse.

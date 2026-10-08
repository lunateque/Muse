# muse-text

The text half of the mood search: a tiny HTTP service that turns a phrase into a 768-number vector
in the same space as the sound vectors in the catalog (EmbeddingGemma 2, text tower only, CPU).
`muse` calls `POST http://127.0.0.1:8911/embed {"text": "..."}` and gets `{"vec": [...]}`
(normalised, the `query` prompt). It answers about 40-80 ms per phrase and holds about 2 GB of RAM.
While it is down, `/api/mood-search` answers 503 and the rest of Muse is unaffected.

The server reads its address from `[moods] text_url` in `muse.toml` (default `http://127.0.0.1:8911`).

## Install

`deploy/provision.sh` creates the `muse-text` user, `/opt/muse-text` with its own virtualenv
(CPU torch and sentence-transformers from `requirements.txt`), copies `app.py` there and enables
`muse-text.service`. The model weights are not downloaded on the server. Copy them from the PC's
Hugging Face cache, where the embedding tool already pulled them:

    # on the PC (Windows: %USERPROFILE%\.cache\huggingface\hub)
    tar -czf embeddinggemma-2.tar.gz -C ~/.cache/huggingface/hub models--google--embeddinggemma-2
    scp embeddinggemma-2.tar.gz server:/tmp/

    # on the server
    install -d -o muse-text -g muse-text /opt/muse-text/hf/hub
    tar -xzf /tmp/embeddinggemma-2.tar.gz -C /opt/muse-text/hf/hub
    chown -R muse-text:muse-text /opt/muse-text/hf
    systemctl start muse-text

The unit sets `HF_HOME=/opt/muse-text/hf` and `HF_HUB_OFFLINE=1`, so it never reaches for the
network. Check it with:

    curl -s -X POST http://127.0.0.1:8911/embed -d '{"text": "slow rainy night"}' | head -c 120

The vectors in the catalog must come from the same model; after a model change re-embed the
catalog (`scripts/embed_catalog.py`) before restarting the service.

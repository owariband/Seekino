// The pinned llama.cpp headers own the ABI of all by-value structs.
#include "llama.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

enum { CONTEXT = 8192, DIMENSIONS = 1024 };
struct encoder {
    struct llama_model *model;
    struct llama_context *ctx;
};

API int qwen_init(const char *runtime_dir) {
#ifdef _WIN32
    // Official Windows distributions have dynamically selected CPU kernels.
    ggml_backend_load_all_from_path(runtime_dir);
#else
    (void)runtime_dir;
#endif
    if (!ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_CPU)) return -1;
    llama_backend_init();
    return 0;
}

API void qwen_close(struct encoder *e) {
    if (!e) return;
    if (e->ctx) llama_free(e->ctx);
    if (e->model) llama_model_free(e->model);
    free(e);
}

API struct encoder *qwen_open(const char *path) {
    struct encoder *e = calloc(1, sizeof(*e));
    if (!e) return NULL;
    struct llama_model_params model_params = llama_model_default_params();
    ggml_backend_dev_t cpu_only[] = { NULL };
    model_params.devices = cpu_only;
    model_params.n_gpu_layers = 0;
    e->model = llama_model_load_from_file(path, model_params);
    if (e->model && llama_model_n_embd_out(e->model) == DIMENSIONS) {
        struct llama_context_params p = llama_context_default_params();
        p.n_ctx = CONTEXT;
        p.n_batch = CONTEXT;
        p.n_ubatch = 256;
        p.n_threads = 2;
        p.n_threads_batch = 2;
        p.embeddings = true;
        p.pooling_type = LLAMA_POOLING_TYPE_LAST;
        p.offload_kqv = false;
        p.op_offload = false;
        e->ctx = llama_init_from_model(e->model, p);
    }
    if (e->ctx) return e;
    qwen_close(e);
    return NULL;
}

static bool cancelled(void *data) {
    return atomic_load((atomic_int *)data) != 0;
}

API void qwen_cancel(atomic_int *flag) {
    atomic_store(flag, 1);
}

// Positive return is the consumed token count. No silent input truncation.
API int qwen_encode(struct encoder *e, const char *text, int length, float *out, atomic_int *flag) {
    llama_token tokens[CONTEXT];
    int count = llama_tokenize(llama_model_get_vocab(e->model), text, length, tokens, CONTEXT, true, true);
    if (count < 0) return -2;
    if (!count || cancelled(flag)) return -1;
    llama_memory_clear(llama_get_memory(e->ctx), true);
    llama_set_abort_callback(e->ctx, cancelled, flag);
    int status = llama_decode(e->ctx, llama_batch_get_one(tokens, count));
    llama_set_abort_callback(e->ctx, NULL, NULL);
    if (status != 0) return -1;
    const float *embedding = llama_get_embeddings_seq(e->ctx, 0);
    if (!embedding) return -1;
    memcpy(out, embedding, DIMENSIONS * sizeof(*out));
    return count;
}

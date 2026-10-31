/* Minimal JSON tokenizer (jsmn-style token array) with lookup helpers. No allocation. */
#ifndef TC_JSON_H
#define TC_JSON_H
#include <stddef.h>
#include <stdint.h>

typedef enum { TJ_OBJ = 1, TJ_ARR, TJ_STR, TJ_PRIM } tj_type;
typedef struct {
    tj_type type;
    int start, end;   /* byte range; for strings without the quotes */
    int size;         /* objects: number of keys; arrays: number of items; strings used as keys: 1 */
} tj_tok;

/* Parse into toks. Returns the number of tokens, or a negative value on error or overflow. */
int tj_parse(const char *js, size_t len, tj_tok *toks, int max);
int tj_skip(const tj_tok *toks, int i);                                  /* index after the subtree at i */
int tj_get(const char *js, const tj_tok *toks, int obj, const char *key); /* value token of key, or -1 */
int tj_item(const tj_tok *toks, int arr, int n);                          /* n-th element of an array, or -1 */
/* Copy the unescaped string at token t into out (NUL terminated). Returns length, or -1 if it does not fit. */
int tj_str(const char *js, const tj_tok *t, char *out, size_t cap);
int tj_int(const char *js, const tj_tok *t, long *out);                   /* 0 ok */
int tj_bool(const char *js, const tj_tok *t, int *out);                   /* 0 ok */
int tj_is_null(const char *js, const tj_tok *t);
#endif

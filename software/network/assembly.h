#ifndef ASSEMBLY_H
#define ASSEMBLY_H

#include "json.h"
#include "http_request.h"

class Assembly
{
    JSON *presets;
    int socket_fd;
    HTTPReqMessage response;
    char hostname[64];
    char client_id[16];

    int   connect_to_server(void);
    void  close_connection(void);
    JSON *receive_json(void);
public:
    Assembly() {
        presets = NULL;
        socket_fd = -1;
        strcpy(hostname, "hackerswithstyle.se");
        strcpy(client_id, "Ultimate");
    }
    ~Assembly() {
        if (presets)
            delete presets;
    }
    void *get_user_context() { return response.userContext; }
    bool same_server(const char *host, const char *client) const {
        return strcmp(hostname, host) == 0 && strcmp(client_id, client) == 0;
    }
    void  set_server(const char *host, const char *client, bool clear_presets = true);
    void  clear_presets(void) {
        if (presets) {
            delete presets;
            presets = NULL;
        }
    }
    JSON *get_presets(void);
    JSON *send_query(const char *query);
    JSON *request_entries(const char *id, int cat);
    void  request_binary(const char *id, int cat, int idx);
    void  request_binary(const char *path, const char *filename);
};

extern Assembly assembly;

#endif // ASSEMBLY_H

/*
 * iec_log.h - the Software IEC failure log.
 *
 * The drive writes one line for a command channel command that leaves an error, for an
 * open that fails, and for the first failure of a channel, and nothing for an operation
 * that succeeds. Each line starts with SOFTIEC_LOG_PREFIX and carries the bytes the host
 * sent, so an incompatibility with a program can be diagnosed from a device log without
 * a special build. Logging only failures keeps it off the path of every successful
 * command, open and byte.
 *
 * The two formatters below take an explicit length and never treat IEC data as a C
 * string, so an embedded zero, a carriage return and a shifted PETSCII character all
 * survive into the log and stay distinguishable from each other.
 */
#ifndef IEC_LOG_H
#define IEC_LOG_H

#include <stdint.h>

// Shared by every line. Grep for it in a device log.
#define SOFTIEC_LOG_PREFIX "SoftIEC: "

// The command buffer and a file name hold up to 254 bytes, more than a log line should
// carry, so a payload is rendered up to this many bytes; a longer one is cut, the cut is
// marked with "..", and the line still reports the real length (SI-152).
#define SOFTIEC_LOG_MAX_BYTES 64

// Room a caller has to provide for the two renderings of SOFTIEC_LOG_MAX_BYTES.
#define SOFTIEC_LOG_HEX_SIZE  (3 * SOFTIEC_LOG_MAX_BYTES + 4)
#define SOFTIEC_LOG_TEXT_SIZE (4 * SOFTIEC_LOG_MAX_BYTES + 4)

static const char softiec_log_digits[] = "0123456789ABCDEF";

// Renders len bytes as two upper case hex digits each, separated by single spaces.
// Writes at most out_size - 1 characters and always terminates. A rendering that did
// not fit ends in ".." so a reader can tell a short line from a short payload.
// Returns the number of characters written, not counting the terminator.
static inline int softiec_log_hex(const uint8_t *data, int len, char *out, int out_size)
{
    int w = 0;
    if (!out || (out_size < 1)) {
        return 0;
    }
    out[0] = 0;
    if (!data || (len < 0)) {
        return 0;
    }
    for (int i = 0; i < len; i++) {
        int need = (w ? 3 : 2);
        if ((w + need) >= (out_size - 2)) { // keep room for ".." and the terminator
            if ((w + 2) < out_size) {
                out[w++] = '.';
                out[w++] = '.';
            }
            break;
        }
        if (w) {
            out[w++] = ' ';
        }
        out[w++] = softiec_log_digits[(data[i] >> 4) & 15];
        out[w++] = softiec_log_digits[data[i] & 15];
    }
    out[w] = 0;
    return w;
}

// Renders len bytes as readable text. A printable ASCII byte stands for itself; a
// carriage return, a line feed and a zero get the usual short escapes, and every
// other byte, which includes shifted PETSCII and binary parameters, is written as
// \xNN. Same truncation rule and return value as softiec_log_hex().
static inline int softiec_log_text(const uint8_t *data, int len, char *out, int out_size)
{
    int w = 0;
    if (!out || (out_size < 1)) {
        return 0;
    }
    out[0] = 0;
    if (!data || (len < 0)) {
        return 0;
    }
    for (int i = 0; i < len; i++) {
        uint8_t b = data[i];
        char esc[4];
        int n = 0;
        switch (b) {
        case 0x00: esc[0] = '\\'; esc[1] = '0'; n = 2; break;
        case 0x0A: esc[0] = '\\'; esc[1] = 'n'; n = 2; break;
        case 0x0D: esc[0] = '\\'; esc[1] = 'r'; n = 2; break;
        case '"':  esc[0] = '\\'; esc[1] = '"'; n = 2; break;
        case '\\': esc[0] = '\\'; esc[1] = '\\'; n = 2; break;
        default:
            if ((b >= 0x20) && (b < 0x7F)) {
                esc[0] = (char)b;
                n = 1;
            } else {
                esc[0] = '\\';
                esc[1] = 'x';
                esc[2] = softiec_log_digits[(b >> 4) & 15];
                esc[3] = softiec_log_digits[b & 15];
                n = 4;
            }
            break;
        }
        if ((w + n) >= (out_size - 2)) {
            if ((w + 2) < out_size) {
                out[w++] = '.';
                out[w++] = '.';
            }
            break;
        }
        for (int k = 0; k < n; k++) {
            out[w++] = esc[k];
        }
    }
    out[w] = 0;
    return w;
}

#endif /* IEC_LOG_H */

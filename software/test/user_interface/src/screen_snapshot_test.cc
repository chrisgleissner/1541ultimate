#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "screen.h"

extern "C" void outbyte(int b)
{
    char c = (char)b;
    fwrite(&c, 1, 1, stdout);
}

static int fail(const char *message)
{
    puts(message);
    return 1;
}

static int expect_bytes(const char *label, const uint8_t *actual, const uint8_t *expected, int len)
{
    if (memcmp(actual, expected, len) == 0) {
        return 0;
    }
    printf("Unexpected %s bytes.\n", label);
    for (int i = 0; i < len; i++) {
        printf("%02X%s", actual[i], (i == len - 1) ? "\n" : " ");
    }
    return 1;
}

int main()
{
    uint8_t chars[6] = { 0x01, 0x82, 0x03, 0x44, 0x05, 0xC6 };
    uint8_t colors[6] = { 0x10, 0x21, 0x32, 0x43, 0xF4, 0x8F };
    uint8_t out_chars[6] = { 0 };
    uint8_t out_colors[6] = { 0 };
    int width = 0;
    int height = 0;

    Screen_MemMappedCharMatrix screen((char *)(void *)chars, (char *)(void *)colors, 3, 2);
    if (!screen.copy_matrix(out_chars, out_colors, 6, &width, &height)) {
        return fail("copy_matrix unexpectedly failed.");
    }
    if ((width != 3) || (height != 2)) {
        return fail("Unexpected matrix dimensions.");
    }
    if (expect_bytes("character", out_chars, chars, 6)) {
        return 1;
    }
    if (expect_bytes("colour", out_colors, colors, 6)) {
        return 1;
    }

    width = 0;
    height = 0;
    if (screen.copy_matrix(out_chars, out_colors, 5, &width, &height)) {
        return fail("copy_matrix succeeded with an undersized destination.");
    }
    if ((width != 3) || (height != 2)) {
        return fail("Undersized copy did not report matrix dimensions.");
    }

    puts("screen_snapshot_test: OK");
    return 0;
}

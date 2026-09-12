/*
 * StreamTextLog.h
 *
 *  Created on: May 26, 2015
 *      Author: Gideon
 */

#ifndef IO_STREAM_STREAM_TEXTLOG_H_
#define IO_STREAM_STREAM_TEXTLOG_H_

#include "stream.h"
#include "small_printf.h"


class StreamTextLog
{
    static void _put(char c, void **param) {
    	((StreamTextLog *)param)->charout((int)c);
    }
    char *buffer;
    bool buffer_owned;
    int offset;
    int size;
    bool enabled;
public:
    StreamTextLog(int size) {
    	buffer = new char[size];
        buffer_owned = true;
    	this->size = size - 4;
    	offset = 0;
    	enabled = true;
    }

    StreamTextLog(int size, char *existing) {
    	buffer = existing;
        buffer_owned = false;
    	this->size = size - 4;
    	offset = 0;
    	enabled = true;
    }

    ~StreamTextLog() {
        if (buffer_owned) {
    	    delete buffer;
        }
    }

    // Every task writes here without a lock, and an interrupt handler or the code that runs
    // before the scheduler may print too, where no lock can be taken. So the position is
    // read once and checked before it is used: two writers may lose a character to each
    // other, but neither writes outside the buffer (CR-7).
    void charout(int c) {
        int o = offset;
        if ((o < 0) || (o >= size)) {
            o = 0; // clear!
        }
        if (!enabled) {
            offset = o;
            return;
        }
        if (c == 27) {
    		c = '<';
    	} else if (c == 9) {
    		c = ' ';
    	} else if ((c < 32) && (c != 10) && (c != 13)) {
            offset = o;
    		return;
    	}
        buffer[o] = (char)c;
        offset = o + 1;
    }

    // A string longer than the log keeps its end.
    void raw(const char *data) {
        int len = strlen(data);
        if (len > size) {
            data += len - size;
            len = size;
        }
        int o = offset;
        if ((o < 0) || (o + len > size)) {
            o = 0; // clear!
        }
        if (!enabled) {
            offset = o;
            return;
        }
        memcpy(buffer + o, data, len);
        offset = o + len;
    }

    char *getText(void) {
    	buffer[offset] = 0;
    	return buffer;
    }

    int getLength(void) {
    	return offset;
    }

    int format_ap(const char *fmt, va_list ap) {
        return _my_vprintf(StreamTextLog :: _put, (void **)this, fmt, ap);
    }

    int format(const char *fmt, ...) {
        va_list ap;
        int ret;

        va_start(ap, fmt);
        ret = _my_vprintf(StreamTextLog :: _put, (void **)this, fmt, ap);
        va_end(ap);

        return (ret);
    }

    void Reset(void) {
    	offset = 0;
    	enabled = true;
    }

    void Stop(void) {
    	enabled = false;
    }
};


#endif /* IO_STREAM_STREAM_TEXTLOG_H_ */

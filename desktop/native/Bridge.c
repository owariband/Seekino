#include <Python.h>
#include "Bridge.h"

#include <AudioToolbox/AudioToolbox.h>
#include <CoreVideo/CoreVideo.h>
#include <stdio.h>
#include <string.h>

struct SeekinoDecoder {
    PyObject *value;
};

static PyObject *dispatch;

static void assign_error(char **error, const char *message) {
    if (error != NULL) *error = strdup(message);
}

static void python_error(char **error) {
    PyObject *exception = PyErr_GetRaisedException();
    PyObject *message = exception != NULL ? PyObject_Str(exception) : NULL;
    const char *utf8 = message != NULL ? PyUnicode_AsUTF8(message) : NULL;
    assign_error(error, utf8 != NULL ? utf8 : "Python 调用失败");
    Py_XDECREF(message);
    Py_XDECREF(exception);
    PyErr_Clear();
}

static PyStatus add_path(PyConfig *config, const char *resources, const char *suffix) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s%s", resources, suffix) >= (int)sizeof(path))
        return PyStatus_Error("Bundle resource path is too long");
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (wide == NULL) return PyStatus_NoMemory();
    PyStatus status = PyWideStringList_Append(&config->module_search_paths, wide);
    PyMem_RawFree(wide);
    return status;
}

bool seekino_initialize(const char *resources, char **error) {
    if (Py_IsInitialized()) return true;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.install_signal_handlers = 0;
    config.parse_argv = 0;
    config.site_import = 0;
    config.write_bytecode = 0;
    config.module_search_paths_set = 1;
    const char *paths[] = {"", "/python/lib/python3.12", "/python/lib/python3.12/lib-dynload", "/site-packages"};
    char home[4096];
    PyStatus status = snprintf(home, sizeof(home), "%s/python", resources) >= (int)sizeof(home)
        ? PyStatus_Error("Bundle resource path is too long")
        : PyConfig_SetBytesString(&config, &config.home, home);
    for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]) && !PyStatus_Exception(status); i++) {
        status = add_path(&config, resources, paths[i]);
        if (PyStatus_Exception(status)) break;
    }
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        assign_error(error, status.err_msg != NULL ? status.err_msg : "无法初始化随包 Python");
        return false;
    }
    PyEval_SaveThread();
    return true;
}

char *seekino_call(const char *request, char **error) {
    PyGILState_STATE gil = PyGILState_Ensure();
    if (dispatch == NULL) {
        PyObject *module = PyImport_ImportModule("server.desktop.app");
        if (module != NULL) {
            dispatch = PyObject_GetAttrString(module, "dispatch");
            Py_DECREF(module);
        }
    }
    PyObject *out = dispatch != NULL ? PyObject_CallFunction(dispatch, "s", request) : NULL;
    const char *utf8 = out != NULL ? PyUnicode_AsUTF8(out) : NULL;
    char *result = utf8 != NULL ? strdup(utf8) : NULL;
    if (result == NULL) python_error(error);
    Py_XDECREF(out);
    PyGILState_Release(gil);
    return result;
}

SeekinoDecoder *seekino_decoder_open(const char *path, char **error) {
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *module = PyImport_ImportModule("server.desktop.playback");
    PyObject *type = module != NULL ? PyObject_GetAttrString(module, "PlaybackDecoder") : NULL;
    PyObject *value = type != NULL ? PyObject_CallFunction(type, "s", path) : NULL;
    SeekinoDecoder *decoder = NULL;
    if (value != NULL) {
        decoder = malloc(sizeof(*decoder));
        if (decoder != NULL) {
            decoder->value = value;
        } else {
            Py_DECREF(value);
            PyErr_NoMemory();
        }
    }
    if (decoder == NULL) python_error(error);
    Py_XDECREF(type);
    Py_XDECREF(module);
    PyGILState_Release(gil);
    return decoder;
}

bool seekino_decoder_seek(SeekinoDecoder *decoder, int64_t millis, char **error) {
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *result = PyObject_CallMethod(decoder->value, "seek", "L", (long long)millis);
    if (result == NULL) python_error(error);
    Py_XDECREF(result);
    PyGILState_Release(gil);
    return result != NULL;
}

static bool copy_plane(PyObject *plane, int stride, CVPixelBufferRef pixels, size_t index, size_t columns) {
    Py_buffer source;
    size_t rows = CVPixelBufferGetHeightOfPlane(pixels, index);
    size_t dst_stride = CVPixelBufferGetBytesPerRowOfPlane(pixels, index);
    if (stride <= 0 || (size_t)stride < columns || dst_stride < columns ||
        PyObject_GetBuffer(plane, &source, PyBUF_SIMPLE) < 0) return false;
    bool valid = (size_t)source.len >= (size_t)stride * rows;
    if (valid) {
        unsigned char *target = CVPixelBufferGetBaseAddressOfPlane(pixels, index);
        const unsigned char *bytes = source.buf;
        for (size_t row = 0; row < rows; row++)
            memcpy(target + row * dst_stride, bytes + row * (size_t)stride, columns);
    }
    PyBuffer_Release(&source);
    return valid;
}

static CMSampleBufferRef video_sample(PyObject *sample) {
    int kind, width, height, ys, uvs;
    long long pts;
    PyObject *y, *uv;
    if (!PyArg_ParseTuple(sample, "iLiiOiOi", &kind, &pts, &width, &height, &y, &ys, &uv, &uvs))
        return NULL;
    CVPixelBufferRef image = NULL;
    CMVideoFormatDescriptionRef format = NULL;
    CMSampleBufferRef buffer = NULL;
    OSStatus status = CVPixelBufferCreate(NULL, width, height, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                                        NULL, &image);
    if (status != noErr) return NULL;
    if (CVPixelBufferLockBaseAddress(image, 0) == kCVReturnSuccess) {
        bool copied = copy_plane(y, ys, image, 0, (size_t)width)
                      && copy_plane(uv, uvs, image, 1, CVPixelBufferGetWidthOfPlane(image, 1) * 2);
        CVPixelBufferUnlockBaseAddress(image, 0);
        if (copied && CMVideoFormatDescriptionCreateForImageBuffer(NULL, image, &format) == noErr) {
            CMSampleTimingInfo timing = {kCMTimeInvalid, CMTimeMake(pts, 1000000), kCMTimeInvalid};
            CMSampleBufferCreateReadyWithImageBuffer(NULL, image, format, &timing, &buffer);
            CFRelease(format);
        }
    }
    CFRelease(image);
    return buffer;
}

static CMSampleBufferRef audio_sample(PyObject *sample) {
    int kind, count;
    long long pts;
    Py_buffer bytes;
    if (!PyArg_ParseTuple(sample, "iLiy*", &kind, &pts, &count, &bytes)) return NULL;
    CMBlockBufferRef data = NULL;
    CMAudioFormatDescriptionRef format = NULL;
    CMSampleBufferRef buffer = NULL;
    AudioStreamBasicDescription pcm = {
        .mSampleRate = 48000, .mFormatID = kAudioFormatLinearPCM,
        .mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
        .mBytesPerPacket = 4, .mFramesPerPacket = 1, .mBytesPerFrame = 4,
        .mChannelsPerFrame = 2, .mBitsPerChannel = 16
    };
    if (count > 0 && bytes.len == (Py_ssize_t)count * pcm.mBytesPerFrame &&
        CMBlockBufferCreateWithMemoryBlock(NULL, NULL, bytes.len, NULL, NULL, 0, bytes.len,
                                         kCMBlockBufferAssureMemoryNowFlag, &data) == noErr &&
        CMBlockBufferReplaceDataBytes(bytes.buf, data, 0, bytes.len) == noErr &&
        CMAudioFormatDescriptionCreate(NULL, &pcm, 0, NULL, 0, NULL, NULL, &format) == noErr) {
        CMAudioSampleBufferCreateReadyWithPacketDescriptions(NULL, data, format, count,
                                                           CMTimeMake(pts, 48000), NULL, &buffer);
    }
    if (format != NULL) CFRelease(format);
    if (data != NULL) CFRelease(data);
    PyBuffer_Release(&bytes);
    return buffer;
}

CMSampleBufferRef seekino_decoder_next(SeekinoDecoder *decoder, int *track, char **error) {
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *sample = PyObject_CallMethod(decoder->value, "next_sample", NULL);
    CMSampleBufferRef buffer = NULL;
    if (sample == NULL) {
        python_error(error);
    } else if (sample != Py_None) {
        *track = (int)PyLong_AsLong(PyTuple_GetItem(sample, 0));
        buffer = *track == 0 ? video_sample(sample) : audio_sample(sample);
        if (buffer == NULL) {
            if (PyErr_Occurred()) python_error(error);
            else assign_error(error, "无法构造音画播放缓冲区");
        }
    }
    Py_XDECREF(sample);
    PyGILState_Release(gil);
    return buffer;
}

void seekino_decoder_close(SeekinoDecoder *decoder) {
    if (decoder == NULL) return;
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *closed = PyObject_CallMethod(decoder->value, "close", NULL);
    Py_XDECREF(closed);
    PyErr_Clear();
    Py_DECREF(decoder->value);
    PyGILState_Release(gil);
    free(decoder);
}

void seekino_display_immediately(CMSampleBufferRef sample) {
    CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, true);
    if (attachments != NULL && CFArrayGetCount(attachments) != 0) {
        CFMutableDictionaryRef entry = (CFMutableDictionaryRef)CFArrayGetValueAtIndex(attachments, 0);
        CFDictionarySetValue(entry, kCMSampleAttachmentKey_DisplayImmediately, kCFBooleanTrue);
    }
}

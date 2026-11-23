#pragma once

#include <CoreMedia/CoreMedia.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct SeekinoDecoder SeekinoDecoder;

bool seekino_initialize(const char *resources, char **error);
char *seekino_call(const char *request, char **error);
SeekinoDecoder *seekino_decoder_open(const char *path, char **error);
bool seekino_decoder_seek(SeekinoDecoder *decoder, int64_t millis, char **error);
CMSampleBufferRef seekino_decoder_next(SeekinoDecoder *decoder, int *track, char **error) CF_RETURNS_RETAINED;
void seekino_decoder_close(SeekinoDecoder *decoder);
void seekino_display_immediately(CMSampleBufferRef sample);

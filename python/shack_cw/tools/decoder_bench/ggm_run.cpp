// Feed raw float32 mono audio from a file to ggmorse (MIT, ggerganov);
// print "t<TAB>text" each time it outputs characters.
#include "ggmorse/ggmorse.h"
#include <cstdio>
#include <cstring>
#include <vector>
#include <cstdlib>
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: ggm_run file.f32 rate [freq_hz]\n"); return 1; }
    FILE *f = fopen(argv[1], "rb"); if (!f) return 2;
    float rate = atof(argv[2]);
    float fhz = argc > 3 ? atof(argv[3]) : -1.0f;
    std::vector<float> x; float buf[4096]; size_t n;
    while ((n = fread(buf, 4, 4096, f)) > 0) x.insert(x.end(), buf, buf + n);
    fclose(f);
    x.resize(x.size() + (size_t)(3 * rate), 0.0f);
    GGMorse::Parameters p{rate, rate, GGMorse::kDefaultSamplesPerFrame, GGMORSE_SAMPLE_FORMAT_F32, GGMORSE_SAMPLE_FORMAT_F32};
    GGMorse g(p);
    auto pd = g.getDefaultParametersDecode(); pd.frequency_hz = fhz; pd.speed_wpm = -1.0f; g.setParametersDecode(pd);
    // decode() keeps pulling until the callback runs dry, so the callback
    // is only given up to `limit`, which moves on 0.1 s per call: each
    // letter is then timed within 0.1 s of when ggmorse printed it.
    size_t pos = 0, limit = 0, step = (size_t)(0.1 * rate);
    GGMorse::CBWaveformInp cb = [&](void *data, uint32_t nb) -> uint32_t {
        size_t ns = nb / 4; if (pos + ns > limit) return 0;
        memcpy(data, x.data() + pos, nb); pos += ns; return nb; };
    GGMorse::TxRx rx;
    while (true) {
        size_t before = pos;
        if (limit >= x.size()) break;
        limit = std::min(x.size(), limit + step);
        g.decode(cb);
        int k = g.takeRxData(rx);
        // ggmorse itself printf()s each letter to stdout; ours goes to stderr.
        if (k > 0) { fprintf(stderr, "%.3f\t", pos / rate); for (auto c : rx) fputc(c == '\n' ? '|' : c, stderr); fputc('\n', stderr); }
        (void) before;
    }
    return 0;
}

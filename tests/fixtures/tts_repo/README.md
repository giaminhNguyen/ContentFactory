# ToneSpeak

A tiny offline text-to-speech command line tool (test fixture for the TTS Analyzer — not a real engine).

## Install

```
pip install -r requirements.txt
```

## Usage

```
python infer.py --text "Xin chao" --out hello.wav --voice alice
```

ToneSpeak supports Vietnamese and English voices. Input is limited: maximum of 500 characters per request.
Output is a 24 kHz mono WAV file. Speed control is supported with `--speed`. Streaming is not available.

Optional cloud voices need an account; `export TONESPEAK_API_KEY=your-key` before running.

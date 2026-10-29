# Research material (not committed)

Fetch Samsung's TV camera module and the upstream loader:

```bash
mkdir -p research/emp && cd research/emp
curl -LO http://oempprd.samsungcloudsolution.net/emp/emp/X10P_0.970_empCamera_VER_1.140419.zip
unzip -d x10p X10P_0.970_empCamera_VER_1.140419.zip   # libMoIPCamera_2058.so, mgfw_2058.bin
cd ../.. && git clone --depth 1 https://github.com/ondrej-zary/samsung-tvcam-fwloader third_party/samsung-tvcam-fwloader
```

`research/disasm/` holds disassembly listings produced by `tools/disasm.py`.

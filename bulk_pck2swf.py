import sys
from pathlib import Path
import os
from pck2swf import Conv
from rpck import Pck
import traceback


PCKS = Path("./pcks")
SWFS = Path("./swfs")


for pck in os.listdir(PCKS):
    pckf = open(PCKS / Path(pck), "rb"); p =  Pck(pckf.read()); pckf.close()
    try:
        cvx = Conv(p, True)
        out, warn = cvx.convert(True)

        for w in warn:
            print(w)
        outp = SWFS / Path(f"{Path(pck).name}.swf")
        outf = open(outp, "wb"); outf.write(out); outf.close()
    except Exception as e:
        traceback.print_exc()
        print(pck)

    
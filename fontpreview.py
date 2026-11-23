import sys,struct; sys.path.insert(0,'/home/claude/w/tool')
import swfr, tess
from pck2swf import *
from PIL import Image, ImageDraw, ImageChops
def render(cv, fidx, text, S=2, sc=0.045, width=640):
    p=cv.p; pl=payload_of(cv.font(fidx)); fid,fl,lang,nl=struct.unpack_from('<HBBB',pl); o=5+nl; n=struct.unpack_from('<H',pl,o)[0]; o+=2
    base=o; offs=struct.unpack_from('<%dI'%(n+1),pl,o)
    def parse(k):
        r=swfr.BR(pl,base+offs[k]); r.ub(4); r.ub(4); x=y=0; cs=[]; cur=None
        while True:
            if r.ub(1)==0:
                f=r.ub(5)
                if f==0: break
                if f&1:
                    nb=r.ub(5); x=r.sb(nb); y=r.sb(nb)
                    if cur: cs.append(cur)
                    cur=[(x,y)]
                if f&2: r.ub(1)
                if f&4: r.ub(1)
            elif r.ub(1):
                nb=r.ub(4)+2
                if r.ub(1): dx=r.sb(nb); dy=r.sb(nb)
                elif r.ub(1): dx=0; dy=r.sb(nb)
                else: dx=r.sb(nb); dy=0
                x+=dx; y+=dy; cur.append((x,y))
            else:
                nb=r.ub(4)+2; cx=x+r.sb(nb); cy=y+r.sb(nb); ax=cx+r.sb(nb); ay=cy+r.sb(nb); cur+=tess.flatten_q((x,y),(cx,cy),(ax,ay)); x,y=ax,ay
        if cur: cs.append(cur)
        return [c for c in cs if len(c)>=3]
    chars=p.read_font(p.ptrs[fidx])['chars']; cto=base+offs[n]; o2=cto+2*n; adv=struct.unpack_from('<%dh'%n,pl,o2+6)
    im=Image.new('L',(width*S,50*S),0); xo=8*S
    for ch in text:
        if ord(ch) not in chars: xo+=300*sc*S; continue
        gi=chars.index(ord(ch)); mask=Image.new('1',im.size,0)
        for c in parse(gi):
            m=Image.new('1',im.size,0); ImageDraw.Draw(m).polygon([(xo+px*sc*S,36*S+py*sc*S) for px,py in c],fill=1); mask=ImageChops.logical_xor(mask,m)
        im.paste(255,mask=mask.convert('L')); xo+=adv[gi]*sc*S
    return im.resize((width,50),Image.LANCZOS)

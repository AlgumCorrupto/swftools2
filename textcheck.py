import sys,struct; sys.path.insert(0,'/home/claude/w/tool')
from fontpreview import *
import swfr, tess
def load_glyphs(cv,fidx):
    pl=payload_of(cv.font(fidx)); fid,fl,lang,nl=struct.unpack_from('<HBBB',pl); o=5+nl; n=struct.unpack_from('<H',pl,o)[0]; o+=2; base=o; offs=struct.unpack_from('<%dI'%(n+1),pl,o)
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
    return parse
def render_text(cv, ti, W_=640, H_=120, S=2, maxlines=6, ox=0, oy=0):
    p=cv.p; t=p.read_text(p.ptrs[ti]); im=Image.new('L',(W_*S,H_*S),0); x=y=0; font=size=None; fonts={}; lines=0
    for r in t['recs']:
        if r[0]=='font':
            font,size=r[1],r[2]
            if font not in fonts: fonts[font]=load_glyphs(cv,font)
        elif r[0]=='x': x=r[1]
        elif r[0]=='y': y=r[1]; lines+=1
        elif r[0]=='glyphs' and lines<=maxlines:
            cx=x
            for gi,adv in r[1]:
                mask=Image.new('1',im.size,0)
                for c in fonts[font](gi):
                    m=Image.new('1',im.size,0); ImageDraw.Draw(m).polygon([((ox+cx+px*size/1024)/20*S,(oy+y+py*size/1024)/20*S) for px,py in c],fill=1); mask=ImageChops.logical_xor(mask,m)
                im.paste(255,mask=mask.convert('L')); cx+=adv
    return im.resize((W_,H_),Image.LANCZOS)
if __name__=='__main__':
    p=Pck('/mnt/user-data/uploads/legal.pck'); cv=Conv(p,False,GlyphSrc())
    a=render_text(cv,10,H_=150,maxlines=12)
    p2=Pck('/mnt/user-data/uploads/loading_network_t05.pck'); cv2=Conv(p2,False,GlyphSrc())
    ti=[i for i,x in enumerate(p2.ptrs) if x and p2.u8(x)==6][0]
    b=render_text(cv2,ti,H_=80,ox=0,oy=1000)
    out=Image.new('L',(640,a.size[1]+b.size[1]+4)); out.paste(a,(0,0)); out.paste(b,(0,a.size[1]+4)); out.save('/home/claude/w/prev/text_check2.png')

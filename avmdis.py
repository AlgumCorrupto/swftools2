import struct
NAMES={0x96:'push',0x88:'constpool',0x1c:'getvar',0x1d:'setvar',0x4e:'getmember',0x4f:'setmember',0x0a:'add',0x0b:'sub',0x0c:'mul',0x0d:'div',0x12:'not',0x99:'jump',0x9d:'if',0x07:'stop',0x06:'play',0x17:'pop',0x3d:'callfunc',0x52:'callmethod',0x50:'inc',0x51:'dec',0x47:'add2',0x48:'lt2',0x49:'eq2',0x67:'gt2?',0x0e:'eq',0x10:'and',0x11:'or',0x9f:'gotoframe2',0x81:'gotoframe',0x20:'settarget2',0x3e:'return',0x3a:'delete',0x40:'newobj',0x43:'initobj',0x42:'initarray',0x4a:'tonum',0x4b:'tostr',0x1e:'?',0x9b:'deffunc',0x8e:'deffunc2',0x3c:'deflocal',0x3b:'deflocal2',0x28:'swap?'}
def dis(code):
    b=code[2:]; i=0; out=[]; pool=[]
    def val(d,j):
        t=d[j]
        if t==0: e=d.index(0,j+1); return repr(d[j+1:e].decode('latin1')),e+1
        if t==1: return 'f%g'%struct.unpack_from('<f',d,j+1),j+5
        if t==2: return 'null',j+1
        if t==3: return 'undef',j+1
        if t==4: return 'reg%d'%d[j+1],j+2
        if t==5: return 'bool%d'%d[j+1],j+2
        if t==6: return 'd%g'%struct.unpack_from('<d',d,j+1)[0] if False else 'd%g'%struct.unpack('<d',d[j+5:j+9]+d[j+1:j+5])[0],j+9
        if t==7: return 'i%d'%struct.unpack_from('<i',d,j+1),j+5
        if t==8: return pool[d[j+1]] if d[j+1]<len(pool) else 'c%d'%d[j+1],j+2
        if t==9: return pool[struct.unpack_from('<H',d,j+1)[0]],j+3
        return '?%d'%t,len(d)
    while b[i]:
        op=b[i]; pos=i
        if op<0x80: out.append((pos,NAMES.get(op,hex(op)))); i+=1; continue
        n=struct.unpack_from('<H',b,i+1)[0]; d=b[i+3:i+3+n]
        if op==0x88:
            c=struct.unpack_from('<H',d)[0]; pool=[s.decode('latin1') for s in d[2:].split(b'\0')[:c]]; out.append((pos,'constpool %s'%pool))
        elif op==0x96:
            j=0; vs=[]
            while j<len(d): v,j=val(d,j); vs.append(v)
            out.append((pos,'push '+', '.join(vs)))
        elif op in(0x99,0x9d):
            off=struct.unpack_from('<h',d)[0]; out.append((pos,'%s -> %d'%(NAMES[op],i+3+n+off)))
        else: out.append((pos,'%s %s'%(NAMES.get(op,hex(op)),d.hex())))
        i+=3+n
    return out

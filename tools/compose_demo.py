"""Create a labeled three-device presentation using only public synthetic screenshots."""
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont,ImageFilter

ROOT=Path(__file__).resolve().parents[1]


def font(size,bold=False):
    directory=Path('C:/Windows/Fonts')
    path=directory/('msyhbd.ttc' if bold else 'msyh.ttc')
    if not path.exists():raise RuntimeError('Install Microsoft YaHei or adjust the font path for this optional composition')
    return ImageFont.truetype(str(path),size)


def main():
    width,height=1800,1120
    canvas=Image.new('RGB',(width,height))
    pixels=canvas.load()
    for y in range(height):
        for x in range(width):
            glow=max(0,1-((x-250)**2+(y-140)**2)**.5/1200)
            pixels[x,y]=(int(12+7*glow),int(15+6*glow),int(24+19*glow))
    draw=ImageDraw.Draw(canvas)
    draw.text((66,42),'AGENT NEXUS',font=font(29,True),fill='#a6b8ff')
    draw.text((66,96),'让电脑继续工作，让你自由移动。',font=font(48,True),fill='#f0f3ff')
    draw.text((69,174),'同一套工作环境 · 任何移动设备的浏览器',font=font(23),fill='#a3adc4')

    def device(filename,x,y,w,h,label):
        shadow=Image.new('RGBA',canvas.size)
        sd=ImageDraw.Draw(shadow);sd.rounded_rectangle((x-7,y+4,x+w+7,y+h+15),radius=21,fill=(0,0,0,170))
        shadow=shadow.filter(ImageFilter.GaussianBlur(20));canvas.paste(shadow,(0,0),shadow)
        screen=Image.open(ROOT/'docs/images'/filename).convert('RGB').resize((w,h),Image.Resampling.LANCZOS)
        frame=Image.new('RGB',(w+12,h+12),'#2b3347');frame.paste(screen,(6,6))
        mask=Image.new('L',frame.size,0);ImageDraw.Draw(mask).rounded_rectangle((0,0,w+11,h+11),radius=15,fill=255)
        canvas.paste(frame,(x-6,y-6),mask)
        label_x=x+88 if filename=='tablet.png' else x
        ImageDraw.Draw(canvas).text((label_x,y+h+23),label,font=font(20),fill='#b5bfd4')

    device('desktop.png',66,267,1036,728,'电脑端 · 项目、工具与会话')
    device('tablet.png',1184,267,540,527,'平板端 · 保留双栏视图')
    device('mobile.png',1010,505,236,512,'手机端 · 随时查看与接管')
    draw=ImageDraw.Draw(canvas)
    draw.text((69,1070),'完整前端渲染 / 虚构演示数据 / 不含私人对话',font=font(17),fill='#7e8aa4')
    canvas.save(ROOT/'docs/images/overview.png')
    print('Saved docs/images/overview.png')


if __name__=='__main__':main()

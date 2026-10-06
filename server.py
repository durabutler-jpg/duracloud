import os, uuid, jwt, boto3, random, hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, or_, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

DB_URL=os.getenv("DURA_DATABASE_URL","sqlite:///./dura_cloud.db")
SECRET=os.getenv("DURA_SECRET","CHANGE-ME")
R2_ENDPOINT=os.getenv("R2_ENDPOINT","")
R2_ACCESS_KEY_ID=os.getenv("R2_ACCESS_KEY_ID","")
R2_SECRET_ACCESS_KEY=os.getenv("R2_SECRET_ACCESS_KEY","")
R2_BUCKET=os.getenv("R2_BUCKET_NAME","duratube-media")
OPENAI_API_KEY=os.getenv("DURAIA_OPENAI_API_KEY","")
OPENAI_MODEL=os.getenv("DURAIA_MODEL","gpt-6-astra")

engine=create_engine(DB_URL,connect_args={"check_same_thread":False} if DB_URL.startswith("sqlite") else {},pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
Base=declarative_base(); ph=PasswordHasher()

class User(Base):
    __tablename__="users"
    id=Column(Integer,primary_key=True); address=Column(String(255),unique=True,index=True,nullable=False)
    display_name=Column(String(80),nullable=False); channel_name=Column(String(80),nullable=False,default="")
    password_hash=Column(String(255),nullable=False); is_admin=Column(Boolean,default=False)
    is_official=Column(Boolean,default=False); is_banned=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)
class Mail(Base):
    __tablename__="mail"
    id=Column(Integer,primary_key=True); sender_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    recipient_id=Column(Integer,ForeignKey("users.id"),nullable=False); subject=Column(String(180),default="")
    body=Column(Text,default=""); is_read=Column(Boolean,default=False); is_starred=Column(Boolean,default=False)
    trash_sender=Column(Boolean,default=False); trash_recipient=Column(Boolean,default=False); created_at=Column(DateTime,default=datetime.utcnow)
class Video(Base):
    __tablename__="videos"
    id=Column(Integer,primary_key=True); owner_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
    title=Column(String(180),nullable=False); description=Column(Text,default=""); channel=Column(String(80),nullable=False)
    object_key=Column(String(500),nullable=False); thumbnail_key=Column(String(500),nullable=True)
    is_short=Column(Boolean,default=False); status=Column(String(30),default="published")
    views=Column(Integer,default=0); likes=Column(Integer,default=0); is_featured=Column(Boolean,default=False); created_at=Column(DateTime,default=datetime.utcnow)
class VideoLike(Base):
    __tablename__="video_likes"; __table_args__=(UniqueConstraint("video_id","user_id",name="uq_video_like"),)
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
class Subscription(Base):
    __tablename__="subscriptions"; __table_args__=(UniqueConstraint("subscriber_id","channel_id",name="uq_subscription"),)
    id=Column(Integer,primary_key=True); subscriber_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); channel_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
class Comment(Base):
    __tablename__="comments"
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    content=Column(Text,nullable=False); created_at=Column(DateTime,default=datetime.utcnow)
class Post(Base):
    __tablename__="posts"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); content=Column(Text,default="")
    media_key=Column(String(500),nullable=True); media_type=Column(String(20),default="none"); likes=Column(Integer,default=0); created_at=Column(DateTime,default=datetime.utcnow)
class PostLike(Base):
    __tablename__="post_likes"; __table_args__=(UniqueConstraint("post_id","user_id",name="uq_post_like"),)
    id=Column(Integer,primary_key=True); post_id=Column(Integer,ForeignKey("posts.id"),nullable=False); user_id=Column(Integer,ForeignKey("users.id"),nullable=False)
class Promotion(Base):
    __tablename__="promotions"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False)
    budget_cents=Column(Integer,default=0); target_impressions=Column(Integer,default=0); delivered_impressions=Column(Integer,default=0); status=Column(String(30),default="active"); created_at=Column(DateTime,default=datetime.utcnow)
class ChannelProfile(Base):
    __tablename__="channel_profiles"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False)
    description=Column(Text,default=""); logo_key=Column(String(500),nullable=True); banner_key=Column(String(500),nullable=True)
    accent=Column(String(20),default="#7c5cff"); font=Column(String(80),default="Segoe UI"); layout=Column(String(30),default="modern")
class ThemeProfile(Base):
    __tablename__="theme_profiles"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False)
    accent=Column(String(20),default="#ff3158"); background=Column(String(20),default="#0f0f10"); surface=Column(String(20),default="#18181b")
    text=Column(String(20),default="#f6f6f7"); font=Column(String(80),default="Segoe UI"); radius=Column(Integer,default=16); density=Column(String(20),default="comfortable"); graphic=Column(String(30),default="clean")
class AiAccess(Base):
    __tablename__="ai_access"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False); verified=Column(Boolean,default=False); verified_at=Column(DateTime,nullable=True)
class AiCode(Base):
    __tablename__="ai_codes"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); code_hash=Column(String(64),nullable=False); expires_at=Column(DateTime,nullable=False); used=Column(Boolean,default=False)

Base.metadata.create_all(engine)
app=FastAPI(title="Dura Cloud",version="3.0")

def db():
    d=SessionLocal()
    try:yield d
    finally:d.close()
def clean_address(a):
    a=a.strip().lower()
    if "@" not in a:a+="@duramail"
    if not a.endswith("@duramail"):raise HTTPException(400,"Adresse @duramail obligatoire.")
    return a
def make_token(u):return jwt.encode({"sub":str(u.id),"exp":datetime.now(timezone.utc)+timedelta(days=30)},SECRET,algorithm="HS256")
def me(authorization:str=Header(default=""),d:Session=Depends(db)):
    if not authorization.startswith("Bearer "):raise HTTPException(401,"Connexion requise.")
    try:uid=int(jwt.decode(authorization[7:],SECRET,algorithms=["HS256"])["sub"])
    except Exception:raise HTTPException(401,"Session invalide.")
    u=d.get(User,uid)
    if not u or u.is_banned:raise HTTPException(403,"Compte indisponible.")
    return u
def pub_user(u):return {"id":u.id,"address":u.address,"email":u.address,"name":u.display_name,"username":u.channel_name or u.display_name,"channel":u.channel_name or "","has_channel":bool(u.channel_name),"admin":bool(u.is_admin),"official":bool(u.is_official)}
def r2():
    if not all([R2_ENDPOINT,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET]):raise HTTPException(503,"Stockage média non configuré.")
    return boto3.client("s3",endpoint_url=R2_ENDPOINT,aws_access_key_id=R2_ACCESS_KEY_ID,aws_secret_access_key=R2_SECRET_ACCESS_KEY,region_name="auto")
def signed(key):return r2().generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":key},ExpiresIn=3600)
def pub_video(v):return {"id":v.id,"title":v.title,"description":v.description or "","owner_id":v.owner_id,"channel":v.channel,"views":v.views or 0,"likes":v.likes or 0,"short":bool(v.is_short),"featured":bool(v.is_featured),"media_url":f"/videos/{v.id}/file","thumbnail_url":f"/videos/{v.id}/thumbnail" if v.thumbnail_key else None,"created_at":v.created_at.isoformat() if v.created_at else ""}

class Register(BaseModel):
    address:str; display_name:str=Field(min_length=2,max_length=80); password:str=Field(min_length=8,max_length=128); channel_name:Optional[str]=""
class Login(BaseModel):address:str;password:str
class SendMail(BaseModel):to:str;subject:str="";body:str=""
class ThemeIn(BaseModel):accent:str="#ff3158";background:str="#0f0f10";surface:str="#18181b";text:str="#f6f6f7";font:str="Segoe UI";radius:int=16;density:str="comfortable";graphic:str="clean"
class ChannelIn(BaseModel):name:str=Field(min_length=2,max_length=80);description:str=""
class VideoEdit(BaseModel):title:Optional[str]=None;description:Optional[str]=None;status:Optional[str]=None
class AiVerify(BaseModel):code:str
class AiChat(BaseModel):message:str=Field(min_length=1,max_length=12000);history:list[dict]=[]

@app.on_event("startup")
def bootstrap():
    d=SessionLocal()
    try:
        u=d.query(User).filter(User.address=="admin@duramail").first()
        if not u:
            pw=os.getenv("DURA_ADMIN_PASSWORD","Dura-Admin-ChangeMe-2026!")
            u=User(address="admin@duramail",display_name="DuraIndustry",channel_name="DuraTube",password_hash=ph.hash(pw),is_admin=True,is_official=True);d.add(u);d.commit();d.refresh(u)
        else:u.is_admin=True;u.is_official=True;u.channel_name=u.channel_name or "DuraTube";d.commit()
        if not d.query(ChannelProfile).filter_by(user_id=u.id).first():d.add(ChannelProfile(user_id=u.id,description="Chaîne officielle Dura."));d.commit()
    finally:d.close()

@app.get("/")
def status():return {"service":"Dura Cloud","version":"3.0","status":"online","duratube":True,"studio":True,"duramail":True,"duraia":True}
@app.post("/auth/register")
def register(x:Register,d:Session=Depends(db)):
    a=clean_address(x.address)
    if d.query(User).filter(User.address==a).first():raise HTTPException(409,"Cette adresse @duramail existe déjà.")
    ch=(x.channel_name or "").strip()
    if ch and d.query(User).filter(User.channel_name.ilike(ch)).first():raise HTTPException(409,"Ce nom de chaîne existe déjà.")
    u=User(address=a,display_name=x.display_name.strip(),channel_name=ch,password_hash=ph.hash(x.password));d.add(u);d.commit();d.refresh(u)
    d.add(ThemeProfile(user_id=u.id));
    if ch:d.add(ChannelProfile(user_id=u.id))
    official=d.query(User).filter(User.is_official==True).first()
    if official and official.id!=u.id:d.add(Subscription(subscriber_id=u.id,channel_id=official.id))
    d.commit();return {"token":make_token(u),"user":pub_user(u)}
@app.post("/auth/login")
def login(x:Login,d:Session=Depends(db)):
    u=d.query(User).filter(User.address==clean_address(x.address)).first()
    if not u:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    try:ok=ph.verify(u.password_hash,x.password)
    except VerifyMismatchError:ok=False
    if not ok:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    if u.is_banned:raise HTTPException(403,"Compte suspendu.")
    return {"token":make_token(u),"user":pub_user(u)}
@app.get("/auth/me")
def auth_me(u:User=Depends(me)):return pub_user(u)

@app.get("/theme/me")
def theme_me(u:User=Depends(me),d:Session=Depends(db)):
    t=d.query(ThemeProfile).filter_by(user_id=u.id).first()
    if not t:t=ThemeProfile(user_id=u.id);d.add(t);d.commit();d.refresh(t)
    return {k:getattr(t,k) for k in ["accent","background","surface","text","font","radius","density","graphic"]}
@app.put("/theme/me")
def theme_save(x:ThemeIn,u:User=Depends(me),d:Session=Depends(db)):
    t=d.query(ThemeProfile).filter_by(user_id=u.id).first() or ThemeProfile(user_id=u.id)
    for k,v in x.model_dump().items():setattr(t,k,v)
    d.add(t);d.commit();return {"ok":True}

@app.post("/channel/create")
def create_channel(x:ChannelIn,u:User=Depends(me),d:Session=Depends(db)):
    if u.channel_name:raise HTTPException(409,"Tu as déjà une chaîne.")
    if d.query(User).filter(User.channel_name.ilike(x.name.strip())).first():raise HTTPException(409,"Nom de chaîne déjà utilisé.")
    u.channel_name=x.name.strip();p=ChannelProfile(user_id=u.id,description=x.description[:2000]);d.add(p);d.commit();return pub_user(u)
@app.get("/channel/me")
def channel_me(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(404,"Aucune chaîne.")
    p=d.query(ChannelProfile).filter_by(user_id=u.id).first() or ChannelProfile(user_id=u.id)
    if not p.id:d.add(p);d.commit();d.refresh(p)
    return pub_user(u)|{"description":p.description or "","accent":p.accent,"font":p.font,"layout":p.layout,"logo_url":f"/channel/{u.id}/logo" if p.logo_key else None,"banner_url":f"/channel/{u.id}/banner" if p.banner_key else None,"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count()}
@app.get("/channel/{uid}")
def channel(uid:int,d:Session=Depends(db)):
    u=d.get(User,uid)
    if not u or not u.channel_name:raise HTTPException(404)
    p=d.query(ChannelProfile).filter_by(user_id=uid).first() or ChannelProfile(user_id=uid)
    return pub_user(u)|{"description":p.description or "","accent":p.accent,"font":p.font,"layout":p.layout,"logo_url":f"/channel/{uid}/logo" if p.logo_key else None,"banner_url":f"/channel/{uid}/banner" if p.banner_key else None,"subscribers":d.query(Subscription).filter_by(channel_id=uid).count(),"videos":d.query(Video).filter_by(owner_id=uid,status="published").count()}
@app.post("/channel/assets")
async def channel_assets(description:str=Form(""),accent:str=Form("#7c5cff"),font:str=Form("Segoe UI"),layout:str=Form("modern"),logo:UploadFile|None=File(None),banner:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Crée une chaîne d'abord.")
    p=d.query(ChannelProfile).filter_by(user_id=u.id).first() or ChannelProfile(user_id=u.id)
    p.description=description[:2000];p.accent=accent[:20];p.font=font[:80];p.layout=layout[:30]
    for f,attr,prefix in [(logo,"logo_key","logos"),(banner,"banner_key","banners")]:
        if f and f.filename:
            ext=Path(f.filename).suffix.lower()
            if ext not in {".jpg",".jpeg",".png",".webp"}:raise HTTPException(400,"Image non acceptée.")
            key=f"channels/{u.id}/{prefix}/{uuid.uuid4().hex}{ext}";r2().upload_fileobj(f.file,R2_BUCKET,key,ExtraArgs={"ContentType":f.content_type or "image/jpeg"});setattr(p,attr,key)
    d.add(p);d.commit();return {"ok":True}
@app.get("/channel/{uid}/logo")
def channel_logo(uid:int,d:Session=Depends(db)):
    p=d.query(ChannelProfile).filter_by(user_id=uid).first()
    if not p or not p.logo_key:raise HTTPException(404)
    return RedirectResponse(signed(p.logo_key))
@app.get("/channel/{uid}/banner")
def channel_banner(uid:int,d:Session=Depends(db)):
    p=d.query(ChannelProfile).filter_by(user_id=uid).first()
    if not p or not p.banner_key:raise HTTPException(404)
    return RedirectResponse(signed(p.banner_key))

@app.post("/mail")
def send_mail(x:SendMail,u:User=Depends(me),d:Session=Depends(db)):
    r=d.query(User).filter(User.address==clean_address(x.to)).first()
    if not r:raise HTTPException(404,"Adresse DuraMail inexistante.")
    d.add(Mail(sender_id=u.id,recipient_id=r.id,subject=x.subject[:180],body=x.body[:100000]));d.commit();return {"ok":True}
@app.get("/mail")
def get_mail(folder:str="inbox",u:User=Depends(me),d:Session=Depends(db)):
    if folder=="sent":rows=d.query(Mail).filter(Mail.sender_id==u.id,Mail.trash_sender==False).order_by(Mail.id.desc()).all()
    elif folder=="trash":rows=d.query(Mail).filter(or_((Mail.sender_id==u.id)&(Mail.trash_sender==True),(Mail.recipient_id==u.id)&(Mail.trash_recipient==True))).order_by(Mail.id.desc()).all()
    else:rows=d.query(Mail).filter(Mail.recipient_id==u.id,Mail.trash_recipient==False).order_by(Mail.id.desc()).all()
    out=[]
    for m in rows:
        s=d.get(User,m.sender_id);r=d.get(User,m.recipient_id)
        out.append({"id":m.id,"from":s.address,"to":r.address,"peer":r.address if m.sender_id==u.id else s.address,"subject":m.subject,"body":m.body,"read":bool(m.is_read),"starred":bool(m.is_starred),"date":m.created_at.strftime("%d/%m/%Y %H:%M")})
    return out
@app.post("/mail/{mid}/read")
def read_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or m.recipient_id!=u.id:raise HTTPException(404)
    m.is_read=True;d.commit();return {"ok":True}
@app.post("/mail/{mid}/star")
def star_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or u.id not in (m.sender_id,m.recipient_id):raise HTTPException(404)
    m.is_starred=not m.is_starred;d.commit();return {"starred":m.is_starred}
@app.delete("/mail/{mid}")
def delete_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m:raise HTTPException(404)
    if m.sender_id==u.id:m.trash_sender=True
    if m.recipient_id==u.id:m.trash_recipient=True
    d.commit();return {"ok":True}

@app.post("/videos/upload")
async def upload_video(title:str=Form(...),description:str=Form(""),is_short:bool=Form(False),file:UploadFile=File(...),thumbnail:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Crée une chaîne avant de publier.")
    ext=Path(file.filename or "").suffix.lower()
    if ext not in {".mp4",".mov",".mkv",".avi",".webm",".m4v"}:raise HTTPException(400,"Format vidéo non accepté.")
    key=f"videos/{u.id}/{uuid.uuid4().hex}{ext}";r2().upload_fileobj(file.file,R2_BUCKET,key,ExtraArgs={"ContentType":file.content_type or "video/mp4"})
    tk=None
    if thumbnail and thumbnail.filename:
        te=Path(thumbnail.filename).suffix.lower()
        if te not in {".jpg",".jpeg",".png",".webp"}:raise HTTPException(400,"Miniature non acceptée.")
        tk=f"thumbnails/{u.id}/{uuid.uuid4().hex}{te}";r2().upload_fileobj(thumbnail.file,R2_BUCKET,tk,ExtraArgs={"ContentType":thumbnail.content_type or "image/jpeg"})
    v=Video(owner_id=u.id,title=title.strip()[:180] or "Sans titre",description=description[:10000],channel=u.channel_name,object_key=key,thumbnail_key=tk,is_short=is_short,is_featured=bool(u.is_official));d.add(v);d.commit();d.refresh(v);return pub_video(v)
@app.get("/videos")
def videos(q:str="",short:int=-1,owner_id:int=0,d:Session=Depends(db)):
    x=d.query(Video).filter(Video.status=="published")
    if q:x=x.filter(Video.title.ilike(f"%{q}%"))
    if short in (0,1):x=x.filter(Video.is_short==bool(short))
    if owner_id:x=x.filter(Video.owner_id==owner_id)
    rows=x.all();rows.sort(key=lambda v:(bool(v.is_featured),v.id),reverse=True);return [pub_video(v) for v in rows]
@app.get("/feed")
def feed(d:Session=Depends(db)):
    rows=d.query(Video).filter(Video.status=="published").all();active={p.video_id:p for p in d.query(Promotion).filter(Promotion.status=="active").all() if p.delivered_impressions<p.target_impressions}
    rows.sort(key=lambda v:((1000000 if v.is_featured else 0)+(500000 if v.id in active else 0)+(v.views or 0)+(v.likes or 0)*5+v.id),reverse=True)
    for v in rows[:30]:
        p=active.get(v.id)
        if p:p.delivered_impressions+=1;p.status="completed" if p.delivered_impressions>=p.target_impressions else "active"
    d.commit();return [pub_video(v)|{"promoted":v.id in active} for v in rows[:30]]
@app.get("/videos/{vid}/file")
def video_file(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    return RedirectResponse(signed(v.object_key))
@app.get("/videos/{vid}/thumbnail")
def video_thumb(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or not v.thumbnail_key:raise HTTPException(404)
    return RedirectResponse(signed(v.thumbnail_key))
@app.post("/videos/{vid}/view")
def view(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    v.views=(v.views or 0)+1;d.commit();return {"views":v.views}
@app.post("/videos/{vid}/like")
def like(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    old=d.query(VideoLike).filter_by(video_id=vid,user_id=u.id).first()
    if old:d.delete(old);v.likes=max(0,(v.likes or 0)-1);liked=False
    else:d.add(VideoLike(video_id=vid,user_id=u.id));v.likes=(v.likes or 0)+1;liked=True
    d.commit();return {"liked":liked,"likes":v.likes}
@app.patch("/videos/{vid}")
def edit_video(vid:int,x:VideoEdit,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or (v.owner_id!=u.id and not u.is_admin):raise HTTPException(404)
    for k,val in x.model_dump(exclude_none=True).items():
        if k=="title":val=val[:180]
        if k=="description":val=val[:10000]
        if k=="status" and val not in {"published","private","unlisted"}:raise HTTPException(400)
        setattr(v,k,val)
    d.commit();return pub_video(v)
@app.delete("/videos/{vid}")
def delete_video(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or (v.owner_id!=u.id and not u.is_admin):raise HTTPException(404)
    try:r2().delete_object(Bucket=R2_BUCKET,Key=v.object_key)
    except Exception:pass
    if v.thumbnail_key:
        try:r2().delete_object(Bucket=R2_BUCKET,Key=v.thumbnail_key)
        except Exception:pass
    d.delete(v);d.commit();return {"ok":True}
@app.get("/videos/{vid}/comments")
def comments(vid:int,d:Session=Depends(db)):
    return [{"id":c.id,"username":d.get(User,c.user_id).channel_name or d.get(User,c.user_id).display_name,"content":c.content,"created_at":c.created_at.isoformat()} for c in d.query(Comment).filter_by(video_id=vid).order_by(Comment.id.desc()).all()]
@app.post("/videos/{vid}/comments")
def add_comment(vid:int,content:str=Form(...),u:User=Depends(me),d:Session=Depends(db)):
    if not d.get(Video,vid):raise HTTPException(404)
    c=Comment(video_id=vid,user_id=u.id,content=content.strip()[:1000])
    if not c.content:raise HTTPException(400)
    d.add(c);d.commit();return {"ok":True}
@app.get("/channels")
def channels(q:str="",d:Session=Depends(db)):
    x=d.query(User).filter(User.is_banned==False,User.channel_name!="")
    if q:x=x.filter(User.channel_name.ilike(f"%{q}%"))
    return [pub_user(u)|{"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count(),"logo_url":f"/channel/{u.id}/logo" if (d.query(ChannelProfile).filter_by(user_id=u.id).first() and d.query(ChannelProfile).filter_by(user_id=u.id).first().logo_key) else None} for u in x.all()]
@app.post("/subscriptions/{channel_id}")
def subscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    if channel_id==u.id:raise HTTPException(400)
    if not d.get(User,channel_id):raise HTTPException(404)
    if not d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first():d.add(Subscription(subscriber_id=u.id,channel_id=channel_id));d.commit()
    return {"subscribed":True}
@app.delete("/subscriptions/{channel_id}")
def unsubscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    s=d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first()
    if s:d.delete(s);d.commit()
    return {"subscribed":False}
@app.get("/studio/dashboard")
def studio_dashboard(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    vids=d.query(Video).filter_by(owner_id=u.id).order_by(Video.id.desc()).all();subs=d.query(Subscription).filter_by(channel_id=u.id).count()
    return {"channel":u.channel_name,"subscribers":subs,"videos":len(vids),"views":sum(v.views or 0 for v in vids),"likes":sum(v.likes or 0 for v in vids),"recent":[pub_video(v) for v in vids[:8]]}
@app.get("/studio/content")
def studio_content(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    return [pub_video(v)|{"status":v.status} for v in d.query(Video).filter_by(owner_id=u.id).order_by(Video.id.desc()).all()]

@app.get("/posts")
def posts(d:Session=Depends(db)):
    out=[]
    for p in d.query(Post).order_by(Post.id.desc()).all():
        usr=d.get(User,p.user_id);out.append({"id":p.id,"user_id":p.user_id,"username":usr.channel_name or usr.display_name,"content":p.content or "","media_type":p.media_type,"media_url":f"/posts/{p.id}/media" if p.media_key else None,"likes":p.likes or 0,"created_at":p.created_at.isoformat()})
    return out
@app.post("/posts")
async def create_post(content:str=Form(""),media:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    key=None;typ="none"
    if media and media.filename:
        ext=Path(media.filename).suffix.lower();typ="image" if ext in {".jpg",".jpeg",".png",".webp",".gif"} else "video" if ext in {".mp4",".mov",".webm",".mkv"} else ""
        if not typ:raise HTTPException(400,"Média non accepté.")
        key=f"posts/{u.id}/{uuid.uuid4().hex}{ext}";r2().upload_fileobj(media.file,R2_BUCKET,key,ExtraArgs={"ContentType":media.content_type or "application/octet-stream"})
    if not content.strip() and not key:raise HTTPException(400,"Post vide.")
    p=Post(user_id=u.id,content=content.strip()[:3000],media_key=key,media_type=typ);d.add(p);d.commit();d.refresh(p);return {"id":p.id}
@app.get("/posts/{pid}/media")
def post_media(pid:int,d:Session=Depends(db)):
    p=d.get(Post,pid)
    if not p or not p.media_key:raise HTTPException(404)
    return RedirectResponse(signed(p.media_key))
@app.post("/posts/{pid}/like")
def post_like(pid:int,u:User=Depends(me),d:Session=Depends(db)):
    p=d.get(Post,pid)
    if not p:raise HTTPException(404)
    old=d.query(PostLike).filter_by(post_id=pid,user_id=u.id).first()
    if old:d.delete(old);p.likes=max(0,(p.likes or 0)-1);liked=False
    else:d.add(PostLike(post_id=pid,user_id=u.id));p.likes=(p.likes or 0)+1;liked=True
    d.commit();return {"liked":liked,"likes":p.likes}

@app.get("/ai/status")
def ai_status(u:User=Depends(me),d:Session=Depends(db)):
    a=d.query(AiAccess).filter_by(user_id=u.id).first();return {"verified":bool(a and a.verified),"provider_ready":bool(OPENAI_API_KEY)}
@app.post("/ai/request-code")
def ai_request_code(u:User=Depends(me),d:Session=Depends(db)):
    code=f"{random.randint(0,999999):06d}";h=hashlib.sha256(code.encode()).hexdigest();d.add(AiCode(user_id=u.id,code_hash=h,expires_at=datetime.utcnow()+timedelta(minutes=10)))
    admin=d.query(User).filter(User.is_official==True).first()
    if not admin:raise HTTPException(500,"Compte système absent.")
    d.add(Mail(sender_id=admin.id,recipient_id=u.id,subject="Code d’accès DuraIA",body=f"Ton code DuraIA est : {code}\nIl expire dans 10 minutes."));d.commit();return {"ok":True,"message":"Code envoyé dans DuraMail."}
@app.post("/ai/verify")
def ai_verify(x:AiVerify,u:User=Depends(me),d:Session=Depends(db)):
    h=hashlib.sha256(x.code.strip().encode()).hexdigest();row=d.query(AiCode).filter_by(user_id=u.id,used=False).order_by(AiCode.id.desc()).first()
    if not row or row.expires_at<datetime.utcnow() or row.code_hash!=h:raise HTTPException(400,"Code invalide ou expiré.")
    row.used=True;a=d.query(AiAccess).filter_by(user_id=u.id).first() or AiAccess(user_id=u.id);a.verified=True;a.verified_at=datetime.utcnow();d.add(a);d.commit();return {"verified":True}
@app.post("/ai/chat")
def ai_chat(x:AiChat,u:User=Depends(me),d:Session=Depends(db)):
    a=d.query(AiAccess).filter_by(user_id=u.id).first()
    if not a or not a.verified:raise HTTPException(403,"Vérifie d’abord ton accès DuraIA.")
    if not OPENAI_API_KEY:return {"text":"DuraIA est activée, mais le modèle IA n’est pas encore relié côté serveur. Ajoute DURAIA_OPENAI_API_KEY dans Render."}
    try:
        from openai import OpenAI
        client=OpenAI(api_key=OPENAI_API_KEY)
        history=(x.history or [])[-20:]
        inp=[{"role":m.get("role","user"),"content":str(m.get("content",""))[:8000]} for m in history]+[{"role":"user","content":x.message}]
        response=client.responses.create(model=OPENAI_MODEL,instructions="Tu es DuraIA, assistant généraliste de l’écosystème Dura. Réponds clairement en français par défaut.",input=inp,max_output_tokens=1800)
        return {"text":response.output_text}
    except Exception as e:raise HTTPException(502,f"Erreur du fournisseur IA: {type(e).__name__}")

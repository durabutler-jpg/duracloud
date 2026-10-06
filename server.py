import os, uuid, jwt, boto3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

DB_URL=os.getenv("DURA_DATABASE_URL","sqlite:///./dura_cloud.db")
SECRET=os.getenv("DURA_SECRET","CHANGE-ME")
R2_ENDPOINT=os.getenv("R2_ENDPOINT","")
R2_ACCESS_KEY_ID=os.getenv("R2_ACCESS_KEY_ID","")
R2_SECRET_ACCESS_KEY=os.getenv("R2_SECRET_ACCESS_KEY","")
R2_BUCKET=os.getenv("R2_BUCKET_NAME","duratube-media")

engine=create_engine(DB_URL,connect_args={"check_same_thread":False} if DB_URL.startswith("sqlite") else {},pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
Base=declarative_base(); ph=PasswordHasher()

def r2():
    if not all([R2_ENDPOINT,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET]):
        raise HTTPException(503,"Stockage média non configuré.")
    return boto3.client("s3",endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto")

class User(Base):
    __tablename__="users"
    id=Column(Integer,primary_key=True); address=Column(String(255),unique=True,index=True,nullable=False)
    display_name=Column(String(80),nullable=False); channel_name=Column(String(80),nullable=False)
    password_hash=Column(String(255),nullable=False); is_admin=Column(Boolean,default=False)
    is_official=Column(Boolean,default=False); is_banned=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)

class Mail(Base):
    __tablename__="mail"
    id=Column(Integer,primary_key=True); sender_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    recipient_id=Column(Integer,ForeignKey("users.id"),nullable=False); subject=Column(String(180),default="")
    body=Column(Text,default=""); is_read=Column(Boolean,default=False); is_starred=Column(Boolean,default=False)
    trash_sender=Column(Boolean,default=False); trash_recipient=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)

class Video(Base):
    __tablename__="videos"
    id=Column(Integer,primary_key=True); owner_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
    title=Column(String(180),nullable=False); description=Column(Text,default=""); channel=Column(String(80),nullable=False)
    object_key=Column(String(500),nullable=False); thumbnail_key=Column(String(500),nullable=True)
    is_short=Column(Boolean,default=False); status=Column(String(30),default="published")
    views=Column(Integer,default=0); likes=Column(Integer,default=0); is_featured=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)

class VideoLike(Base):
    __tablename__="video_likes"
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True)
    user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)

class Subscription(Base):
    __tablename__="subscriptions"
    id=Column(Integer,primary_key=True); subscriber_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
    channel_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)

class Comment(Base):
    __tablename__="comments"
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True)
    user_id=Column(Integer,ForeignKey("users.id"),nullable=False); content=Column(Text,nullable=False)
    created_at=Column(DateTime,default=datetime.utcnow)

class Post(Base):
    __tablename__="posts"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
    content=Column(Text,default=""); media_key=Column(String(500),nullable=True); media_type=Column(String(20),default="none")
    likes=Column(Integer,default=0); created_at=Column(DateTime,default=datetime.utcnow)

class PostLike(Base):
    __tablename__="post_likes"
    id=Column(Integer,primary_key=True); post_id=Column(Integer,ForeignKey("posts.id"),nullable=False,index=True)
    user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)

class Promotion(Base):
    __tablename__="promotions"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    video_id=Column(Integer,ForeignKey("videos.id"),nullable=False); budget_cents=Column(Integer,default=0)
    target_impressions=Column(Integer,default=0); delivered_impressions=Column(Integer,default=0)
    status=Column(String(30),default="active"); created_at=Column(DateTime,default=datetime.utcnow)

Base.metadata.create_all(engine)
app=FastAPI(title="Dura Cloud",version="2.0")

def db():
    x=SessionLocal()
    try: yield x
    finally: x.close()

def clean_address(a):
    a=a.strip().lower()
    if "@" not in a:a+="@duramail"
    if not a.endswith("@duramail"):raise HTTPException(400,"Adresse @duramail obligatoire.")
    return a

def make_token(u):
    return jwt.encode({"sub":str(u.id),"exp":datetime.now(timezone.utc)+timedelta(days=30)},SECRET,algorithm="HS256")

def me(authorization:str=Header(default=""),d:Session=Depends(db)):
    if not authorization.startswith("Bearer "):raise HTTPException(401,"Connexion requise.")
    try:uid=int(jwt.decode(authorization[7:],SECRET,algorithms=["HS256"])["sub"])
    except Exception:raise HTTPException(401,"Session invalide.")
    u=d.get(User,uid)
    if not u or u.is_banned:raise HTTPException(403,"Compte indisponible.")
    return u

def pub_user(u):
    return {"id":u.id,"address":u.address,"email":u.address,"name":u.display_name,"username":u.channel_name,
            "channel":u.channel_name,"admin":bool(u.is_admin),"official":bool(u.is_official)}

def pub_video(v):
    return {"id":v.id,"title":v.title,"description":v.description or "","owner_id":v.owner_id,
            "channel":v.channel,"views":v.views or 0,"likes":v.likes or 0,"status":v.status,
            "featured":bool(v.is_featured),"short":bool(v.is_short),
            "media_url":f"/videos/{v.id}/file","thumbnail_url":f"/videos/{v.id}/thumbnail" if v.thumbnail_key else None,
            "created_at":v.created_at.isoformat() if v.created_at else ""}

class Login(BaseModel): address:str; password:str
class CreateUser(BaseModel):
    address:str; display_name:str=Field(min_length=2,max_length=80)
    channel_name:str=Field(min_length=2,max_length=80); password:str=Field(min_length=8,max_length=128)
class SendMail(BaseModel): to:str; subject:str=""; body:str=""
class PromotionRequest(BaseModel): video_id:int; budget_eur:float=Field(gt=0,le=10000)

@app.on_event("startup")
def bootstrap():
    d=SessionLocal()
    try:
        u=d.query(User).filter(User.address=="admin@duramail").first()
        if not u:
            pw=os.getenv("DURA_ADMIN_PASSWORD","Dura-Admin-ChangeMe-2026!")
            u=User(address="admin@duramail",display_name="DuraIndustry",channel_name="DuraTube",
                   password_hash=ph.hash(pw),is_admin=True,is_official=True)
            d.add(u);d.commit()
        else:
            u.is_admin=True;u.is_official=True;u.channel_name="DuraTube";d.commit()
    finally:d.close()

@app.get("/")
def status():return {"service":"Dura Cloud","version":"2.0","status":"online","duratube":"public-backend"}

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
def who(u:User=Depends(me)):return pub_user(u)

@app.post("/admin/users")
def create_user(x:CreateUser,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403,"Réservé à DuraIndustry.")
    a=clean_address(x.address)
    if d.query(User).filter(User.address==a).first():raise HTTPException(409,"Adresse déjà utilisée.")
    nu=User(address=a,display_name=x.display_name.strip(),channel_name=x.channel_name.strip(),password_hash=ph.hash(x.password))
    d.add(nu);d.commit();d.refresh(nu)
    admin=d.query(User).filter(User.is_official==True).first()
    if admin and admin.id!=nu.id:d.add(Subscription(subscriber_id=nu.id,channel_id=admin.id));d.commit()
    return pub_user(nu)

@app.get("/admin/users")
def users(u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    return [pub_user(x)|{"banned":bool(x.is_banned)} for x in d.query(User).order_by(User.id.desc()).all()]

@app.post("/admin/users/{uid}/ban")
def ban(uid:int,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    target=d.get(User,uid)
    if not target or target.is_official:raise HTTPException(400,"Action impossible.")
    target.is_banned=not target.is_banned;d.commit();return {"banned":target.is_banned}

@app.post("/mail")
def send_mail(x:SendMail,u:User=Depends(me),d:Session=Depends(db)):
    r=d.query(User).filter(User.address==clean_address(x.to)).first()
    if not r:raise HTTPException(404,"Cette adresse DuraMail n'existe pas.")
    d.add(Mail(sender_id=u.id,recipient_id=r.id,subject=x.subject[:180],body=x.body[:100000]));d.commit()
    return {"ok":True}

@app.get("/mail")
def get_mail(folder:str="inbox",u:User=Depends(me),d:Session=Depends(db)):
    if folder=="sent":rows=d.query(Mail).filter(Mail.sender_id==u.id,Mail.trash_sender==False).order_by(Mail.id.desc()).all()
    elif folder=="trash":rows=d.query(Mail).filter(or_((Mail.sender_id==u.id)&(Mail.trash_sender==True),(Mail.recipient_id==u.id)&(Mail.trash_recipient==True))).order_by(Mail.id.desc()).all()
    else:rows=d.query(Mail).filter(Mail.recipient_id==u.id,Mail.trash_recipient==False).order_by(Mail.id.desc()).all()
    out=[]
    for m in rows:
        s=d.get(User,m.sender_id);r=d.get(User,m.recipient_id)
        out.append({"id":m.id,"from":s.address,"to":r.address,"peer":r.address if m.sender_id==u.id else s.address,
                    "subject":m.subject,"body":m.body,"read":bool(m.is_read),"starred":bool(m.is_starred),
                    "date":m.created_at.strftime("%d/%m/%Y %H:%M")})
    return out

@app.post("/mail/{mid}/read")
def mark_read(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or m.recipient_id!=u.id:raise HTTPException(404)
    m.is_read=True;d.commit();return {"ok":True}

@app.post("/mail/{mid}/star")
def star(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or u.id not in (m.sender_id,m.recipient_id):raise HTTPException(404)
    m.is_starred=not m.is_starred;d.commit();return {"starred":m.is_starred}

@app.delete("/mail/{mid}")
def trash(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m:raise HTTPException(404)
    if m.sender_id==u.id:m.trash_sender=True
    if m.recipient_id==u.id:m.trash_recipient=True
    d.commit();return {"ok":True}

@app.post("/videos/upload")
async def upload_video(title:str=Form(...),description:str=Form(""),is_short:bool=Form(False),
                       file:UploadFile=File(...),thumbnail:UploadFile|None=File(None),
                       u:User=Depends(me),d:Session=Depends(db)):
    ext=Path(file.filename or "").suffix.lower()
    if ext not in {".mp4",".mov",".mkv",".avi",".webm",".m4v"}:raise HTTPException(400,"Format vidéo non accepté.")
    key=f"videos/{u.id}/{uuid.uuid4().hex}{ext}"
    client=r2();client.upload_fileobj(file.file,R2_BUCKET,key,ExtraArgs={"ContentType":file.content_type or "video/mp4"})
    thumb_key=None
    if thumbnail and thumbnail.filename:
        te=Path(thumbnail.filename).suffix.lower()
        if te not in {".jpg",".jpeg",".png",".webp"}:raise HTTPException(400,"Miniature non acceptée.")
        thumb_key=f"thumbnails/{u.id}/{uuid.uuid4().hex}{te}"
        client.upload_fileobj(thumbnail.file,R2_BUCKET,thumb_key,ExtraArgs={"ContentType":thumbnail.content_type or "image/jpeg"})
    v=Video(owner_id=u.id,title=(title.strip() or "Sans titre")[:180],description=description[:10000],
            channel=u.channel_name,object_key=key,thumbnail_key=thumb_key,is_short=is_short,
            is_featured=bool(u.is_official),status="published")
    d.add(v);d.commit();d.refresh(v);return pub_video(v)

@app.get("/videos")
def videos(q:str="",short:int=-1,d:Session=Depends(db)):
    query=d.query(Video).filter(Video.status=="published")
    if q:query=query.filter(Video.title.ilike(f"%{q}%"))
    if short in (0,1):query=query.filter(Video.is_short==bool(short))
    rows=query.all()
    rows.sort(key=lambda v:(1 if v.is_featured else 0,v.id),reverse=True)
    return [pub_video(v) for v in rows]

@app.get("/feed")
def feed(d:Session=Depends(db)):
    rows=d.query(Video).filter(Video.status=="published").all()
    active={p.video_id:p for p in d.query(Promotion).filter(Promotion.status=="active").all() if p.delivered_impressions<p.target_impressions}
    rows.sort(key=lambda v:((1000000 if v.is_featured else 0)+(500000 if v.id in active else 0)+min(v.views or 0,10000)+min((v.likes or 0)*5,10000)+v.id),reverse=True)
    for v in rows[:12]:
        p=active.get(v.id)
        if p:
            p.delivered_impressions+=1
            if p.delivered_impressions>=p.target_impressions:p.status="completed"
    d.commit()
    return [pub_video(v)|{"promoted":v.id in active,"official_boost":bool(v.is_featured)} for v in rows[:30]]

@app.get("/videos/{vid}/file")
def video_file(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404,"Vidéo introuvable.")
    url=r2().generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":v.object_key},ExpiresIn=3600)
    return RedirectResponse(url)

@app.get("/videos/{vid}/thumbnail")
def thumbnail(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or not v.thumbnail_key:raise HTTPException(404)
    url=r2().generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":v.thumbnail_key},ExpiresIn=3600)
    return RedirectResponse(url)

@app.post("/videos/{vid}/view")
def view(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    v.views=(v.views or 0)+1;d.commit();return {"views":v.views}

@app.post("/videos/{vid}/like")
def like(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    old=d.query(VideoLike).filter(VideoLike.video_id==vid,VideoLike.user_id==u.id).first()
    if old:d.delete(old);v.likes=max(0,(v.likes or 0)-1);liked=False
    else:d.add(VideoLike(video_id=vid,user_id=u.id));v.likes=(v.likes or 0)+1;liked=True
    d.commit();return {"likes":v.likes,"liked":liked}

@app.get("/videos/{vid}/comments")
def comments(vid:int,d:Session=Depends(db)):
    rows=d.query(Comment).filter(Comment.video_id==vid).order_by(Comment.id.desc()).all()
    return [{"id":c.id,"username":d.get(User,c.user_id).channel_name,"content":c.content,"created_at":c.created_at.isoformat()} for c in rows]

@app.post("/videos/{vid}/comments")
def add_comment(vid:int,content:str=Form(...),u:User=Depends(me),d:Session=Depends(db)):
    if not d.get(Video,vid):raise HTTPException(404)
    if not content.strip():raise HTTPException(400,"Commentaire vide.")
    c=Comment(video_id=vid,user_id=u.id,content=content.strip()[:1000]);d.add(c);d.commit();d.refresh(c)
    return {"id":c.id,"username":u.channel_name,"content":c.content}

@app.get("/channels")
def channels(q:str="",d:Session=Depends(db)):
    query=d.query(User).filter(User.is_banned==False)
    if q:query=query.filter(User.channel_name.ilike(f"%{q}%"))
    out=[]
    for x in query.all():
        out.append(pub_user(x)|{"subscribers":d.query(Subscription).filter(Subscription.channel_id==x.id).count(),
                                "videos":d.query(Video).filter(Video.owner_id==x.id,Video.status=="published").count()})
    return sorted(out,key=lambda x:(x["official"],x["subscribers"]),reverse=True)

@app.post("/subscriptions/{channel_id}")
def subscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    if u.id==channel_id:raise HTTPException(400,"Impossible de s'abonner à soi-même.")
    if not d.get(User,channel_id):raise HTTPException(404)
    old=d.query(Subscription).filter(Subscription.subscriber_id==u.id,Subscription.channel_id==channel_id).first()
    if not old:d.add(Subscription(subscriber_id=u.id,channel_id=channel_id));d.commit()
    return {"subscribed":True}

@app.delete("/subscriptions/{channel_id}")
def unsubscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    s=d.query(Subscription).filter(Subscription.subscriber_id==u.id,Subscription.channel_id==channel_id).first()
    if s:d.delete(s);d.commit()
    return {"ok":True}

@app.get("/subscriptions/status/{channel_id}")
def sub_status(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    yes=d.query(Subscription).filter(Subscription.subscriber_id==u.id,Subscription.channel_id==channel_id).first() is not None
    return {"subscribed":yes,"subscribers":d.query(Subscription).filter(Subscription.channel_id==channel_id).count()}

@app.get("/posts")
def posts(d:Session=Depends(db)):
    rows=d.query(Post).order_by(Post.id.desc()).all()
    return [{"id":x.id,"user_id":x.user_id,"username":d.get(User,x.user_id).channel_name,"content":x.content or "",
             "media_type":x.media_type,"media_url":f"/posts/{x.id}/media" if x.media_key else None,
             "likes":x.likes or 0,"created_at":x.created_at.isoformat()} for x in rows]

@app.post("/posts")
async def create_post(content:str=Form(""),media:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    key=None;typ="none"
    if media and media.filename:
        ext=Path(media.filename).suffix.lower()
        if ext in {".jpg",".jpeg",".png",".webp",".gif"}:typ="image"
        elif ext in {".mp4",".mov",".webm",".mkv"}:typ="video"
        else:raise HTTPException(400,"Média non accepté.")
        key=f"posts/{u.id}/{uuid.uuid4().hex}{ext}"
        r2().upload_fileobj(media.file,R2_BUCKET,key,ExtraArgs={"ContentType":media.content_type or "application/octet-stream"})
    if not content.strip() and not key:raise HTTPException(400,"Post vide.")
    x=Post(user_id=u.id,content=content.strip()[:3000],media_key=key,media_type=typ);d.add(x);d.commit();d.refresh(x)
    return {"id":x.id}

@app.get("/posts/{pid}/media")
def post_media(pid:int,d:Session=Depends(db)):
    x=d.get(Post,pid)
    if not x or not x.media_key:raise HTTPException(404)
    return RedirectResponse(r2().generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":x.media_key},ExpiresIn=3600))

@app.post("/posts/{pid}/like")
def post_like(pid:int,u:User=Depends(me),d:Session=Depends(db)):
    x=d.get(Post,pid)
    if not x:raise HTTPException(404)
    old=d.query(PostLike).filter(PostLike.post_id==pid,PostLike.user_id==u.id).first()
    if old:d.delete(old);x.likes=max(0,(x.likes or 0)-1);liked=False
    else:d.add(PostLike(post_id=pid,user_id=u.id));x.likes=(x.likes or 0)+1;liked=True
    d.commit();return {"liked":liked,"likes":x.likes}

@app.post("/promotions")
def create_promotion(x:PromotionRequest,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,x.video_id)
    if not v:raise HTTPException(404)
    if v.owner_id!=u.id and not u.is_admin:raise HTTPException(403)
    cents=max(100,int(round(x.budget_eur*100)))
    target=(cents//100)*300
    p=Promotion(user_id=u.id,video_id=v.id,budget_cents=cents,target_impressions=target,status="active")
    d.add(p);d.commit();d.refresh(p)
    return {"id":p.id,"target_impressions":target,"status":p.status,
            "note":"Placement recommandé. Les vues et abonnés ne sont jamais falsifiés."}

@app.get("/promotions")
def promotions(u:User=Depends(me),d:Session=Depends(db)):
    rows=d.query(Promotion).filter(Promotion.user_id==u.id).order_by(Promotion.id.desc()).all()
    return [{"id":x.id,"video_id":x.video_id,"budget_eur":x.budget_cents/100,
             "target_impressions":x.target_impressions,"delivered_impressions":x.delivered_impressions,
             "status":x.status} for x in rows]

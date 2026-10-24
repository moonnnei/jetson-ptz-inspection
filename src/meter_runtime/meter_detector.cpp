// Standalone YOLO11 single-class NCNN detector. No camera-control code.
#include "net.h"
#include <opencv2/opencv.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>

struct Box { float x1,y1,x2,y2,score; int anchor; };
static double now_ms(){return std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static int number(const char* text){size_t used=0;int n=std::stoi(text,&used);if(text[used])throw std::invalid_argument("integer");return n;}
static float clip(float x,float hi){return std::max(0.f,std::min(hi,x));}
static float overlap(const Box& a,const Box& b){
    float inter=std::max(0.f,std::min(a.x2,b.x2)-std::max(a.x1,b.x1))*std::max(0.f,std::min(a.y2,b.y2)-std::max(a.y1,b.y1));
    float area=(a.x2-a.x1)*(a.y2-a.y1)+(b.x2-b.x1)*(b.y2-b.y1)-inter;
    return area>0?inter/area:0;
}
static int infer(ncnn::Net& net,const ncnn::Mat& in,ncnn::Mat& out,int size){
    ncnn::Extractor ex=net.create_extractor();
    if(ex.input("in0",in) || ex.extract("out0",out))return 7;
    int expected=(size/8)*(size/8)+(size/16)*(size/16)+(size/32)*(size/32);
    if(out.dims!=2 || out.h!=5 || out.w!=expected){std::cerr<<"Unexpected output layout "<<out.dims<<" "<<out.w<<" "<<out.h<<"\n";return 8;}
    return 0;
}
int main(int argc,char** argv){
    try{
        if(argc!=6 && argc!=9)return 2;
        int size=number(argv[3]),threads=number(argv[4]),rotation=number(argv[5]);
        if(size<128 || size>1280 || size%32 || threads<1 || threads>6 || (rotation!=0 && rotation!=180))return 2;
        cv::setNumThreads(1);
        ncnn::Net net;net.opt.num_threads=threads;net.opt.use_vulkan_compute=false;
        net.opt.use_fp16_storage=false;net.opt.use_fp16_arithmetic=false;net.opt.use_bf16_storage=false;
        if(net.load_param(argv[1]) || net.load_model(argv[2]))return 3;
        if(argc==9){
            if(std::string(argv[6])!="--tensor")return 2;
            ncnn::Mat in(size,size,3);std::ifstream f(argv[7],std::ios::binary);
            size_t n=size_t(size)*size*3*sizeof(float);
            if(!f.read((char*)in.data,n) || f.peek()!=EOF)return 11;
            ncnn::Mat out;int result=infer(net,in,out,size);if(result)return result;
            std::ofstream dest(argv[8],std::ios::binary);int32_t header[]={out.dims,out.w,out.h,out.c};
            dest.write((char*)header,sizeof(header));
            for(int row=0;row<out.h;row++)dest.write((const char*)out.row(row),out.w*sizeof(float));
            return dest?0:12;
        }
        uint32_t n;
        while(std::cin.read((char*)&n,4)){
            if(n<4 || n>12000000)return 4;
            std::vector<uchar> bytes(n);if(!std::cin.read((char*)bytes.data(),n))return 5;
            double t0=now_ms();cv::Mat bgr=cv::imdecode(bytes,cv::IMREAD_COLOR);
            if(bgr.empty() || bgr.cols>8192 || bgr.rows>8192)return 6;
            if(rotation==180)cv::flip(bgr,bgr,-1);
            double t1=now_ms();int w=bgr.cols,h=bgr.rows;
            float ratio=std::min(float(size)/w,float(size)/h);
            int rw=std::round(w*ratio),rh=std::round(h*ratio),left=(size-rw)/2,top=(size-rh)/2;
            cv::Mat resized,padded;cv::resize(bgr,resized,cv::Size(rw,rh));
            cv::copyMakeBorder(resized,padded,top,size-rh-top,left,size-rw-left,cv::BORDER_CONSTANT,cv::Scalar(114,114,114));
            ncnn::Mat in=ncnn::Mat::from_pixels(padded.data,ncnn::Mat::PIXEL_BGR2RGB,size,size);
            float norm[]={1/255.f,1/255.f,1/255.f};in.substract_mean_normalize(0,norm);
            double t2=now_ms();ncnn::Mat out;int result=infer(net,in,out,size);if(result)return result;
            double t3=now_ms();std::vector<Box> boxes,kept;
            const float *cx=out.row(0),*cy=out.row(1),*bw=out.row(2),*bh=out.row(3),*scores=out.row(4);
            for(int i=0;i<out.w;i++){
                if(!std::isfinite(scores[i]) || scores[i]<.25f || !std::isfinite(cx[i]) || !std::isfinite(cy[i]) || !std::isfinite(bw[i]) || !std::isfinite(bh[i]) || bw[i]<=0 || bh[i]<=0)continue;
                Box b{clip((cx[i]-bw[i]*.5f-left)/ratio,float(w)),clip((cy[i]-bh[i]*.5f-top)/ratio,float(h)),
                      clip((cx[i]+bw[i]*.5f-left)/ratio,float(w)),clip((cy[i]+bh[i]*.5f-top)/ratio,float(h)),scores[i],i};
                if(b.x2>b.x1 && b.y2>b.y1)boxes.push_back(b);
            }
            std::stable_sort(boxes.begin(),boxes.end(),[](const Box& a,const Box& b){return a.score>b.score;});
            if(boxes.size()>1000)boxes.resize(1000);
            for(const Box& b:boxes){
                bool suppressed=false;for(const Box& k:kept)if(overlap(b,k)>.45f){suppressed=true;break;}
                if(!suppressed)kept.push_back(b);if(kept.size()>=100)break;
            }
            double t4=now_ms();int pw=std::min(w,960),ph=std::round(double(h)*pw/w);
            cv::Mat preview;cv::resize(bgr,preview,cv::Size(pw,ph));float pr=float(pw)/w;
            for(const Box& b:kept){
                cv::Point a(std::round(b.x1*pr),std::round(b.y1*pr)),z(std::round(b.x2*pr),std::round(b.y2*pr));
                cv::rectangle(preview,a,z,cv::Scalar(40,230,60),2);
                std::ostringstream label;label<<"meter "<<std::round(b.score*100)<<"%";
                cv::putText(preview,label.str(),cv::Point(a.x,std::max(18,a.y-5)),cv::FONT_HERSHEY_SIMPLEX,.55,cv::Scalar(40,230,60),2);
            }
            std::vector<uchar> jpeg;if(!cv::imencode(".jpg",preview,jpeg,{cv::IMWRITE_JPEG_QUALITY,85}))return 9;
            double t5=now_ms();std::ostringstream s;s.precision(9);
            s<<"{\"model\":\"YOLO11n-meter-Pointer10K\",\"input_size\":"<<size<<",\"width\":"<<w<<",\"height\":"<<h<<",\"rotation\":"<<rotation;
            s<<",\"decode_ms\":"<<t1-t0<<",\"preprocess_ms\":"<<t2-t1<<",\"network_ms\":"<<t3-t2<<",\"postprocess_ms\":"<<t4-t3<<",\"preview_ms\":"<<t5-t4<<",\"total_process_ms\":"<<t5-t0;
            s<<",\"confidence_threshold\":0.25,\"nms_iou_threshold\":0.45,\"detections\":[";
            bool first=true;for(const Box& b:kept){
                if(!first)s<<",";first=false;
                s<<"{\"class_id\":0,\"class_name\":\"meter\",\"confidence\":"<<b.score<<",\"bbox\":["<<b.x1/w<<","<<b.y1/h<<","<<b.x2/w<<","<<b.y2/h<<"]}";
            }
            s<<"]}";std::string json=s.str();uint32_t js=json.size(),jpg=jpeg.size();
            std::cout.write((char*)&js,4);std::cout.write(json.data(),js);std::cout.write((char*)&jpg,4);std::cout.write((char*)jpeg.data(),jpg);std::cout.flush();
        }
        return std::cin.eof()?0:10;
    }catch(const std::exception& e){std::cerr<<"Detector failed: "<<e.what()<<"\n";return 20;}
}

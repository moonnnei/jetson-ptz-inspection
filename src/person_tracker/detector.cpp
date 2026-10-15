#include "net.h"
#include <opencv2/opencv.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <exception>
#include <iostream>
#include <sstream>
#include <vector>
static double ms(){return std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static bool readn(char* p,size_t n){return bool(std::cin.read(p,n));}
static bool integer(const char* value,int& result){
    try{size_t used=0;result=std::stoi(value,&used);return value[used]=='\0';}
    catch(const std::exception&){return false;}
}
static void send(const std::string& json,const std::vector<uchar>& jpeg){
    uint32_t j=json.size(),b=jpeg.size();std::cout.write((char*)&j,4);std::cout.write(json.data(),j);
    std::cout.write((char*)&b,4);if(b)std::cout.write((const char*)jpeg.data(),b);std::cout.flush();
}
int main(int argc,char** argv){
    if(argc<4 || argc>6)return 2;
    int rotation=0,threads=4,cv_threads=0;
    if(!integer(argv[3],rotation))return 2;
    if(rotation!=0 && rotation!=90 && rotation!=180 && rotation!=270)return 2;
    if(argc>=5 && (!integer(argv[4],threads) || threads<1 || threads>6))return 2;
    // Preserve the legacy OpenCV default unless explicitly requested. Bounding
    // OpenCV's worker pool can avoid contention with NCNN on the six-core TX2.
    if(argc>=6){
        if(!integer(argv[5],cv_threads) || cv_threads<1 || cv_threads>6)return 2;
        cv::setNumThreads(cv_threads);
    }
    ncnn::Net net;net.opt.num_threads=threads;net.opt.use_vulkan_compute=false;
    net.opt.use_fp16_storage=false;net.opt.use_fp16_arithmetic=false;net.opt.use_bf16_storage=false;
    if(net.load_param(argv[1]) || net.load_model(argv[2]))return 3;
    cv::Mat previous;
    uint32_t header;
    while(readn((char*)&header,4)){
        // Legacy requests contain only the JPEG byte length. Bit 31 opts out
        // of calibration-only optical flow; bit 30 skips optional web preview
        // work. Bit 29 holds the optical-flow reference for direct before /
        // after calibration comparisons. Unflagged requests preserve both
        // legacy output paths and advance the optical-flow reference.
        const bool flow_enabled=(header&0x80000000u)==0;
        const bool preview_generated=(header&0x40000000u)==0;
        const bool flow_reference_held=(header&0x20000000u)!=0;
        if(flow_reference_held && !flow_enabled)return 10;
        const uint32_t n=header&0x1fffffffu;
        if(n<4 || n>12000000)return 4;
        std::vector<uchar> bytes(n);if(!readn((char*)bytes.data(),n))return 5;
        // Processing timings exclude waiting for IPC input and writing output.
        const double process_start=ms();
        cv::Mat bgr=cv::imdecode(bytes,cv::IMREAD_COLOR);if(bgr.empty())return 6;
        if(rotation==180)cv::flip(bgr,bgr,-1);
        else if(rotation==90){cv::transpose(bgr,bgr);cv::flip(bgr,bgr,1);}
        else if(rotation==270){cv::transpose(bgr,bgr);cv::flip(bgr,bgr,0);}
        const double decode_end=ms();
        double start=decode_end;int w=bgr.cols,h=bgr.rows;
        float ratio=std::min(320.f/w,320.f/h);int rw=std::round(w*ratio),rh=std::round(h*ratio);
        int left=(320-rw)/2,top=(320-rh)/2;cv::Mat resized,padded;
        cv::resize(bgr,resized,cv::Size(rw,rh));
        cv::copyMakeBorder(resized,padded,top,320-rh-top,left,320-rw-left,cv::BORDER_CONSTANT,cv::Scalar(114,114,114));
        ncnn::Mat in=ncnn::Mat::from_pixels(padded.data,ncnn::Mat::PIXEL_BGR2RGB,320,320);
        float norm[3]={1/255.f,1/255.f,1/255.f};in.substract_mean_normalize(0,norm);
        const double preprocess_end=ms();
        ncnn::Extractor ex=net.create_extractor();ncnn::Mat out;
        if(ex.input("in0",in) || ex.extract("out0",out))return 7;
        if(out.dims!=2 || out.w!=84)return 8;
        const double network_end=ms();
        double inference=network_end-start;
        cv::Point2d shift(0,0);double response=0;
        // Median sparse optical flow rejects local person motion and avoids
        // phase-correlation's zero-shift bias on compressed surveillance frames.
        if(flow_enabled){
            cv::Mat gray;cv::resize(bgr,gray,cv::Size(640,360));cv::cvtColor(gray,gray,cv::COLOR_BGR2GRAY);
            if(!previous.empty()){
                std::vector<cv::Point2f> a,b;std::vector<uchar> status;std::vector<float> err,dx,dy;
                cv::goodFeaturesToTrack(previous,a,300,0.02,15);
                if(a.size()>=20){
                    cv::calcOpticalFlowPyrLK(previous,gray,a,b,status,err,cv::Size(21,21),3);
                    for(size_t i=0;i<a.size();i++)if(status[i] && err[i]<20){dx.push_back(b[i].x-a[i].x);dy.push_back(b[i].y-a[i].y);}
                    if(dx.size()>=20){
                        std::vector<float> sx=dx,sy=dy;std::sort(sx.begin(),sx.end());std::sort(sy.begin(),sy.end());
                        shift=cv::Point2d(sx[sx.size()/2],sy[sy.size()/2]);
                        int consistent=0;for(size_t i=0;i<dx.size();i++)if(std::hypot(dx[i]-shift.x,dy[i]-shift.y)<2.5)consistent++;
                        response=double(consistent)/dx.size();
                    }
                }
            }
            // Held post-motion frames all compare to the same before-motion
            // image; summing incremental medians can accumulate inconsistent
            // feature sets. An empty held reference is initialized normally.
            if(!flow_reference_held || previous.empty())previous=gray.clone();
        }else{
            // A subsequent calibration must start from a new reference frame,
            // never from a frame preceding tracking/observation or a PTZ move.
            previous.release();
        }
        const double flow_end=ms();
        std::ostringstream persons;persons.precision(7);persons<<"[";
        bool first=true;
        for(int i=0;i<out.h;i++){
            const float* r=out.row(i);float score=r[4]; // COCO person=0
            if(!std::isfinite(score) || score<0.5f)continue;
            float x1=std::max(0.f,std::min(float(w),(r[0]-left)/ratio));
            float y1=std::max(0.f,std::min(float(h),(r[1]-top)/ratio));
            float x2=std::max(0.f,std::min(float(w),(r[2]-left)/ratio));
            float y2=std::max(0.f,std::min(float(h),(r[3]-top)/ratio));
            if(x2<=x1 || y2<=y1)continue;
            if(!first)persons<<",";first=false;
            persons<<"{\"confidence\":"<<score<<",\"bbox\":["<<x1/w<<","<<y1/h<<","<<x2/w<<","<<y2/h<<"]}";
        }
        persons<<"]";
        const double postprocess_end=ms();
        std::vector<uchar> jpeg;double preview_end=postprocess_end;
        if(preview_generated){
            int pw=std::min(w,960),ph=std::round(double(h)*pw/w);cv::Mat preview;cv::resize(bgr,preview,cv::Size(pw,ph));
            if(!cv::imencode(".jpg",preview,jpeg,{cv::IMWRITE_JPEG_QUALITY,80}))return 9;
            preview_end=ms();
        }
        std::ostringstream s;s.precision(7);s<<"{\"width\":"<<w<<",\"height\":"<<h<<",\"inference_ms\":"<<inference;
        s<<",\"decode_ms\":"<<decode_end-process_start<<",\"preprocess_ms\":"<<preprocess_end-start;
        s<<",\"network_ms\":"<<network_end-preprocess_end<<",\"flow_ms\":"<<flow_end-network_end;
        s<<",\"postprocess_ms\":"<<postprocess_end-flow_end<<",\"preview_ms\":"<<preview_end-postprocess_end;
        s<<",\"total_process_ms\":"<<preview_end-process_start<<",\"flow_enabled\":"<<(flow_enabled?"true":"false");
        s<<",\"control_result_ms\":"<<postprocess_end-process_start<<",\"preview_generated\":"<<(preview_generated?"true":"false");
        s<<",\"flow_reference_held\":"<<(flow_reference_held?"true":"false");
        s<<",\"shift\":["<<shift.x/640<<","<<shift.y/360<<"],\"shift_response\":"<<response<<",\"persons\":"<<persons.str()<<"}";
        send(s.str(),jpeg);
    }
}

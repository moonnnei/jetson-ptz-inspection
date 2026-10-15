// YOLO26n NCNN CPU smoke test. No GUI, CUDA, Python or network dependency.
#include "net.h"
#include <opencv2/opencv.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

struct Detection { float x1, y1, x2, y2, score; int label; };
static double now_ms() {
    return std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
static float overlap(const Detection& a, const Detection& b) {
    float w = std::max(0.f, std::min(a.x2,b.x2)-std::max(a.x1,b.x1));
    float h = std::max(0.f, std::min(a.y2,b.y2)-std::max(a.y1,b.y1));
    float intersection=w*h;
    return intersection / std::max(1e-6f, (a.x2-a.x1)*(a.y2-a.y1)+(b.x2-b.x1)*(b.y2-b.y1)-intersection);
}
int main(int argc, char** argv) {
    try {
        if (argc < 6 || argc > 8) {
            std::cerr << "Usage: infer MODEL.param MODEL.bin CLASSES.txt IMAGE OUTPUT_PREFIX [loops=3] [threads=4]\n";
            return 2;
        }
        const int size=320;
        int loops=argc>6?std::stoi(argv[6]):3, threads=argc>7?std::stoi(argv[7]):4;
        if(loops<1 || loops>50 || threads<1 || threads>6) throw std::runtime_error("Invalid loops/threads");
        std::vector<std::string> names;
        std::ifstream classes(argv[3]); std::string name;
        while(std::getline(classes,name)) { if(!name.empty() && name.back()=='\r') name.pop_back(); names.push_back(name); }
        if(names.size()!=80) throw std::runtime_error("Expected 80 COCO class names");
        cv::Mat bgr=cv::imread(argv[4]);
        if(bgr.empty()) throw std::runtime_error("Cannot read image");
        double load_start=now_ms();
        ncnn::Net net;
        net.opt.num_threads=threads;
        net.opt.use_vulkan_compute=false;
        net.opt.use_fp16_storage=false;
        net.opt.use_fp16_arithmetic=false;
        net.opt.use_bf16_storage=false;
        if(net.load_param(argv[1])!=0 || net.load_model(argv[2])!=0) throw std::runtime_error("Cannot load NCNN model");
        double load_ms=now_ms()-load_start;
        double prep_start=now_ms();
        float ratio=std::min(float(size)/bgr.cols,float(size)/bgr.rows);
        int rw=std::round(bgr.cols*ratio), rh=std::round(bgr.rows*ratio);
        int left=(size-rw)/2, top=(size-rh)/2;
        cv::Mat resized, padded;
        cv::resize(bgr,resized,cv::Size(rw,rh),0,0,cv::INTER_LINEAR);
        cv::copyMakeBorder(resized,padded,top,size-rh-top,left,size-rw-left,cv::BORDER_CONSTANT,cv::Scalar(114,114,114));
        ncnn::Mat input=ncnn::Mat::from_pixels(padded.data,ncnn::Mat::PIXEL_BGR2RGB,size,size);
        const float norm[3]={1/255.f,1/255.f,1/255.f};
        input.substract_mean_normalize(0,norm);
        double prep_ms=now_ms()-prep_start;
        ncnn::Mat out;
        std::vector<double> durations;
        // A new extractor per frame, model loaded only once. First inference is warmup.
        for(int i=0;i<loops+1;i++) {
            ncnn::Extractor ex=net.create_extractor();
            double start=now_ms();
            if(ex.input("in0",input)!=0 || ex.extract("out0",out)!=0) throw std::runtime_error("NCNN inference failed");
            if(i>0) durations.push_back(now_ms()-start);
        }
        std::cerr<<"Output dims="<<out.dims<<" w="<<out.w<<" h="<<out.h<<" c="<<out.c<<"\n";
        double post_start=now_ms();
        if(out.dims!=2) throw std::runtime_error("Unexpected output rank; inspect exported model before decoding");
        std::vector<Detection> candidates, detections;
        const float threshold=0.25f;
        bool end2end=(out.w==6 || out.w==84);
        if(out.w==84) {
            // Unmodified one-to-one network; original TopK/Gather performed here.
            // Global top300 scores equals the official two-stage top300 selection.
            for(int i=0;i<out.h;i++) {
                const float* row=out.row(i);
                for(int label=0;label<80;label++) {
                    float score=row[4+label];
                    if(std::isfinite(score) && score>=threshold)
                        candidates.push_back({row[0],row[1],row[2],row[3],score,label});
                }
            }
        } else if(out.w==6) {
            for(int i=0;i<out.h;i++) {
                const float* row=out.row(i);
                if(!std::isfinite(row[4]) || row[4]<threshold) continue;
                int label=std::round(row[5]);
                if(label<0 || label>=80) throw std::runtime_error("Invalid class index");
                candidates.push_back({row[0],row[1],row[2],row[3],row[4],label});
            }
        } else if(out.h==84) {
            for(int i=0;i<out.w;i++) {
                int label=0; float score=out.row(4)[i];
                for(int c=1;c<80;c++) { float v=out.row(4+c)[i]; if(v>score){score=v;label=c;} }
                if(!std::isfinite(score) || score<threshold) continue;
                float x=out.row(0)[i], y=out.row(1)[i], w=out.row(2)[i], h=out.row(3)[i];
                candidates.push_back({x-w/2,y-h/2,x+w/2,y+h/2,score,label});
            }
        } else throw std::runtime_error("Unsupported output layout");
        std::sort(candidates.begin(),candidates.end(),[](const Detection& a,const Detection& b){return a.score>b.score;});
        if(end2end && candidates.size()>300) candidates.resize(300);
        for(Detection d:candidates) {
            bool keep=true;
            if(!end2end) for(const Detection& a:detections) if(a.label==d.label && overlap(a,d)>0.45f){keep=false;break;}
            if(keep) detections.push_back(d);
        }
        for(Detection& d:detections) {
            d.x1=std::max(0.f,std::min(float(bgr.cols),(d.x1-left)/ratio));
            d.x2=std::max(0.f,std::min(float(bgr.cols),(d.x2-left)/ratio));
            d.y1=std::max(0.f,std::min(float(bgr.rows),(d.y1-top)/ratio));
            d.y2=std::max(0.f,std::min(float(bgr.rows),(d.y2-top)/ratio));
        }
        double post_ms=now_ms()-post_start;
        double sum=0; for(double v:durations) sum+=v;
        double mean=sum/durations.size();
        std::string prefix(argv[5]);
        std::ofstream report(prefix+".json");
        if(!report) throw std::runtime_error("Cannot create report");
        report<<std::fixed<<std::setprecision(4);
        report<<"{\n  \"model\":\"YOLO26n\",\"backend\":\"NCNN CPU\",\"imgsz\":320,\"threads\":"<<threads;
        report<<",\"loops\":"<<loops<<",\"warmup\":1,\"end2end\":"<<(end2end?"true":"false");
        report<<",\"image_width\":"<<bgr.cols<<",\"image_height\":"<<bgr.rows;
        report<<",\"load_ms\":"<<load_ms<<",\"preprocess_ms\":"<<prep_ms<<",\"postprocess_ms\":"<<post_ms;
        report<<",\"mean_inference_ms\":"<<mean<<",\"inference_only_fps\":"<<1000/mean;
        report<<",\"output_shape\":["<<out.h<<","<<out.w<<"],\"samples_ms\":[";
        for(size_t i=0;i<durations.size();i++){if(i)report<<",";report<<durations[i];}
        report<<"],\n  \"detections\":[\n";
        for(size_t i=0;i<detections.size();i++) {
            const Detection& d=detections[i];
            if(i) report<<",\n";
            report<<"    {\"class_id\":"<<d.label<<",\"name\":\""<<names[d.label]<<"\",\"confidence\":"<<d.score;
            report<<",\"xyxy\":["<<d.x1<<","<<d.y1<<","<<d.x2<<","<<d.y2<<"]}";
            cv::rectangle(bgr,cv::Point(d.x1,d.y1),cv::Point(d.x2,d.y2),cv::Scalar(0,220,0),2);
            cv::putText(bgr,names[d.label]+" "+std::to_string(d.score).substr(0,4),cv::Point(d.x1,std::max(18.f,d.y1-6)),cv::FONT_HERSHEY_SIMPLEX,0.6,cv::Scalar(0,220,0),2);
        }
        report<<"\n  ]\n}\n";
        report.close();
        if(!cv::imwrite(prefix+".jpg",bgr)) throw std::runtime_error("Cannot save annotated image");
        std::cout<<"detections="<<detections.size()<<" mean_inference_ms="<<mean<<" inference_only_fps="<<1000/mean<<"\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<e.what()<<"\n";return 1; }
}

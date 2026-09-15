/*
 * This code is licensed under the Fastbot license. You may obtain a copy of this license in the LICENSE.txt file in the root directory of this source tree.
 */
/**
 * @authors Jianqiang Guo, Yuhui Su
 */
#include "fastbot_native.h"
#include "Model.h"
#include "ModelReusableAgent.h"
#include "CoverageExporter.h"
#include "utils.hpp"

#ifdef __cplusplus
extern "C" {
#endif

static fastbotx::ModelPtr _fastbot_model = nullptr;

//getAction
jstring JNICALL Java_com_bytedance_fastbot_AiClient_b0bhkadf(JNIEnv *env, jobject, jstring activity,
                                                             jstring xmlDescOfGuiTree) {
    if (nullptr == _fastbot_model) {
        _fastbot_model = fastbotx::Model::create();
    }
    const char *xmlDescriptionCString = env->GetStringUTFChars(xmlDescOfGuiTree, nullptr);
    const char *activityCString = env->GetStringUTFChars(activity, nullptr);
    std::string xmlString = std::string(xmlDescriptionCString);
    std::string activityString = std::string(activityCString);
    std::string operationString = _fastbot_model->getOperate(xmlString, activityString);
    LOGD("do action opt is : %s", operationString.c_str());
    env->ReleaseStringUTFChars(xmlDescOfGuiTree, xmlDescriptionCString);
    env->ReleaseStringUTFChars(activity, activityCString);
    return env->NewStringUTF(operationString.c_str());
}

// for single device, just addAgent as empty device //InitAgent
void JNICALL Java_com_bytedance_fastbot_AiClient_fgdsaf5d(JNIEnv *env, jobject, jint agentType,
                                                          jstring packageName, jint deviceType) {
    if (nullptr == _fastbot_model) {
        _fastbot_model = fastbotx::Model::create();
    }
    auto algorithmType = (fastbotx::AlgorithmType) agentType;
    auto agentPointer = _fastbot_model->addAgent("", algorithmType,
                                                 (fastbotx::DeviceType) deviceType);
    const char *packageNameCString = "";
    if (env)
        packageNameCString = env->GetStringUTFChars(packageName, nullptr);
    _fastbot_model->setPackageName(std::string(packageNameCString));

    BLOG("init agent with type %d, %s,  %d", agentType, packageNameCString, deviceType);
    if (algorithmType == fastbotx::AlgorithmType::Reuse) {
        auto reuseAgentPtr = std::dynamic_pointer_cast<fastbotx::ModelReusableAgent>(agentPointer);
        reuseAgentPtr->loadReuseModel(std::string(packageNameCString));
        if (env)
            env->ReleaseStringUTFChars(packageName, packageNameCString);
    }
}

// load ResMapping
void JNICALL
Java_com_bytedance_fastbot_AiClient_jdasdbil(JNIEnv *env, jobject, jstring resMappingFilepath) {
    if (nullptr == _fastbot_model) {
        _fastbot_model = fastbotx::Model::create();
    }
    const char *resourceMappingPath = env->GetStringUTFChars(resMappingFilepath, nullptr);
    auto preference = _fastbot_model->getPreference();
    if (preference) {
        preference->loadMixResMapping(std::string(resourceMappingPath));
    }
    env->ReleaseStringUTFChars(resMappingFilepath, resourceMappingPath);
}

// to check if a point is in black widget area
jboolean JNICALL
Java_com_bytedance_fastbot_AiClient_nkksdhdk(JNIEnv *env, jobject, jstring activity, jfloat pointX,
                                             jfloat pointY) {
    bool isShield = false;
    if (nullptr == _fastbot_model) {
        BLOGE("%s", "model null, check point failed!");
        return isShield;
    }
    const char *activityStr = env->GetStringUTFChars(activity, nullptr);
    auto preference = _fastbot_model->getPreference();
    if (preference) {
        isShield = preference->checkPointIsInBlackRects(std::string(activityStr),
                                                        static_cast<int>(pointX),
                                                        static_cast<int>(pointY));
    }
    env->ReleaseStringUTFChars(activity, activityStr);
    return isShield;
}

jstring JNICALL Java_com_bytedance_fastbot_AiClient_getNativeVersion(JNIEnv *env, jclass clazz) {
    return env->NewStringUTF(FASTBOT_VERSION);
}

// dump widget-level coverage JSON; runs on the Java decision thread, which is
// the same thread that mutates the Graph via b0bhkadf, so no locking is needed
jstring JNICALL Java_com_bytedance_fastbot_AiClient_dumpCoverage(JNIEnv *env, jclass clazz,
                                                                 jstring packageName) {
    std::string packageNameString;
    if (nullptr != packageName) {
        const char *packageNameCString = env->GetStringUTFChars(packageName, nullptr);
        if (nullptr != packageNameCString) {
            packageNameString = std::string(packageNameCString);
            env->ReleaseStringUTFChars(packageName, packageNameCString);
        }
    }
    if (nullptr == _fastbot_model) {
        _fastbot_model = fastbotx::Model::create();
    }
    std::string coverageJson = fastbotx::CoverageExporter::buildCoverageJson(
            _fastbot_model->getGraph(), packageNameString);
    BDLOG("dump coverage %zu bytes", coverageJson.length());
    return env->NewStringUTF(coverageJson.c_str());
}

#ifdef __cplusplus
}
#endif
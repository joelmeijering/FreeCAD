// SPDX-License-Identifier: LGPL-2.1-or-later

#ifndef APP_DERIVEDDATA_H
#define APP_DERIVEDDATA_H

#include "FCGlobal.h"

#include <string>
#include <unordered_set>
#include <vector>

namespace App
{

class Document;
class DocumentObject;
class Property;

/**
 * Classification and direct-directory persistence for regenerable document data.
 *
 * The classifier is an exact FreeCAD 1.1.1 allowlist: everything not
 * positively recognized remains authored.
 */
class AppExport DerivedData
{
public:
    struct Item
    {
        DocumentObject* object;
        Property* property;
    };

    struct RestoreResult
    {
        std::vector<Item> restored;
        std::vector<Item> missing;
    };

    static std::vector<Item> items(const Document& document, bool requireValue = false);

    static std::string newGeneration();

    /// Publish an immutable saved cache generation.
    static bool saveGeneration(const Document& document,
                               const std::string& fileName,
                               const std::string& generation);

    /// Hydrate an immutable saved cache generation. A bad item is a cache miss.
    static RestoreResult restoreGeneration(Document& document,
                                           const std::string& fileName,
                                           const std::string& generation,
                                           const std::unordered_set<DocumentObject*>& serializedTouched,
                                           const std::unordered_set<DocumentObject*>& forcedMisses);

    /// Atomically replace a saved generation reconstructed from its FCStd.
    static bool refreshGeneration(const Document& document,
                                  const std::string& fileName,
                                  const std::string& generation);

    /// Atomically replace this process's disposable working cache.
    static bool saveWorking(const Document& document, const std::string& fileName);
    static void invalidateWorking(const Document& document, const std::string& fileName);

private:
    DerivedData() = delete;
};

}  // namespace App

#endif  // APP_DERIVEDDATA_H
